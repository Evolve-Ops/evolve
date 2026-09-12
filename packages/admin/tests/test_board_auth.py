"""Tests for the board's credential handling (board_auth + routes_board).

WHAT THESE PIN — the F8 review's fixed findings, each as an executable
claim rather than a note in a document:

  * **F-1 the token never reaches a log line.** Both the direct scrubber and
    a real request round-trip with every logger captured.
  * **F-1/F-5 the token leaves the URL.** ``?t=`` sets an HttpOnly,
    SameSite=Strict, path-scoped cookie and 302s to the clean URL; the clean
    URL then authenticates on its own.
  * **Old bookmarks keep working.** The ``?t=`` form is still accepted
    everywhere, page and API alike — this is an upgrade, not a cutover.
  * **A freshly-minted link wins on its FIRST click**, even on a device
    still holding the cookie for the token just rotated away — the case
    ``docs/help/board.md``'s recovery path sends the operator down.
  * **The 401 oracle is unchanged.** Wrong token, absent token, unminted
    board and unknown bot are still one indistinguishable 401.
  * **F-2 body cap holds without a Content-Length** (chunked framing).
  * **F-3 a cross-origin cookie write is refused** while the identical
    bearer write is allowed (bearer carries no ambient authority).
  * **F-4 writes are rate limited**, and a *successful* request never
    consumes the failed-auth budget.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_store as bs  # noqa: E402
from evolve_admin import telemetry  # noqa: E402
from evolve_admin.web import board_auth, routes_board  # noqa: E402
from evolve_admin.web.board_limits import RateLimiter  # noqa: E402
from evolve_admin.web.routes_board import register_board_routes  # noqa: E402

BOT = "personal-bot"


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    board = bs.load_board(shared, BOT)
    bs.add_card(board, title="Pick the trip weekend", cluster="travel", lane="today")
    bs.save_board(shared, BOT, board)
    token = bs.mint_token(shared, BOT)
    app = Flask(__name__)
    register_board_routes(app, network)
    # Each test gets fresh limiter state: these are module-level singletons
    # in production (one daemon, one process), so a leftover bucket from an
    # earlier test would otherwise leak across cases.
    routes_board._write_limiter = RateLimiter(1000, 60.0)
    routes_board._auth_fail_limiter = RateLimiter(1000, 60.0)
    return {"client": app.test_client(), "shared": shared, "token": token,
            "app": app, "network": network}


def _bearer(pod):
    return {"Authorization": f"Bearer {pod['token']}"}


# ── F-1: the token stays out of every log ──────────────────────────────────

@pytest.mark.parametrize("line", [
    'GET /board/personal-bot?t=SEKRIT HTTP/1.1" 200 -',
    '127.0.0.1 - - "GET /api/board/personal-bot?t=SEKRIT&x=1 HTTP/1.1" 200 -',
    "board page failed for http://pod:5050/board/personal-bot?t=SEKRIT",
])
def test_scrubber_redacts_the_token_value(line):
    out = telemetry.scrub_secrets(line)
    assert "SEKRIT" not in out
    assert telemetry.REDACTED in out


def test_scrubber_leaves_ordinary_lines_alone():
    line = 'GET /api/status HTTP/1.1" 200 - print=1 utm=abc'
    assert telemetry.scrub_secrets(line) == line


def test_scrub_filter_rewrites_a_record_with_args():
    rec = logging.LogRecord(
        "werkzeug", logging.INFO, __file__, 1,
        '"%s" %s %s', ("GET /board/personal-bot?t=SEKRIT HTTP/1.1", 200, "-"),
        None,
    )
    assert telemetry._ScrubFilter().filter(rec) is True
    assert "SEKRIT" not in rec.getMessage()
    # args must be consumed, or a downstream formatter re-expands the secret.
    assert not rec.args


def test_no_logger_sees_the_token_during_a_real_request(pod, caplog):
    """The end-to-end claim: drive the surface the way a phone does and
    assert the credential appears in nothing any handler was offered."""
    handler_records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            handler_records.append(record.getMessage())

    cap = _Capture()
    cap.addFilter(telemetry._ScrubFilter())
    roots = [logging.getLogger(), logging.getLogger("evolve_admin"),
             logging.getLogger("werkzeug")]
    for lg in roots:
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG):
            c = pod["client"]
            c.get(f"/board/{BOT}?t={pod['token']}", follow_redirects=True)
            c.get(f"/api/board/{BOT}?t={pod['token']}")
            c.post(f"/api/board/{BOT}/cards", headers=_bearer(pod),
                   json={"title": "Book dentist", "cluster": "health"})
            c.get(f"/board/{BOT}?t=wrong-token")
    finally:
        for lg in roots:
            lg.removeHandler(cap)

    everything = "\n".join(handler_records + [r.getMessage() for r in caplog.records])
    assert pod["token"] not in everything


# ── the ?t= → cookie upgrade ───────────────────────────────────────────────

def test_query_token_sets_scoped_cookie_and_redirects(pod):
    r = pod["client"].get(f"/board/{BOT}?t={pod['token']}")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/board/{BOT}")
    assert "?t=" not in r.headers["Location"]

    cookies = r.headers.getlist("Set-Cookie")
    assert len(cookies) == 2, "one cookie per board path (page + API)"
    paths = sorted(
        part.split("=", 1)[1]
        for c in cookies for part in c.split("; ") if part.startswith("Path=")
    )
    assert paths == [f"/api/board/{BOT}", f"/board/{BOT}"]
    for c in cookies:
        assert "HttpOnly" in c
        assert "SameSite=Strict" in c
        # Plain HTTP on the tailnet listener — a Secure cookie could not be
        # set there at all (review F-6).
        assert "Secure" not in c
        assert pod["token"] in c  # the cookie IS the credential


def test_clean_url_authenticates_by_cookie(pod):
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")  # sets the cookie
    page = c.get(f"/board/{BOT}")
    assert page.status_code == 200
    assert "<title>Board</title>" in page.get_data(as_text=True)
    api = c.get(f"/api/board/{BOT}")
    assert api.status_code == 200
    assert api.get_json()["cards"]


def test_cookie_is_scoped_off_the_admin_plane(pod):
    """The cookie's Path never covers an admin route, so a board device
    cannot present it anywhere but its own board."""
    for path in board_auth.cookie_paths(BOT):
        assert path.startswith(("/board/", "/api/board/"))
        assert BOT in path


def test_old_bookmark_still_works_on_every_route(pod):
    c = pod["client"]
    assert c.get(f"/api/board/{BOT}?t={pod['token']}").status_code == 200
    assert c.get(f"/board/{BOT}?t={pod['token']}").status_code == 302


def test_a_fresh_link_wins_over_a_stale_cookie_on_the_first_click(pod):
    """The case the operator's phone test hits first.

    ``board token`` rotates, the operator opens the freshly-minted ``?t=``
    link on a phone that still holds the cookie for the rotated-away token.
    The cookie is preferred over ``?t=``, so before the second attempt in
    ``authenticate`` this was a 401 on the FIRST click and a 302 only on the
    second — while both ``docs/help/board.md`` and ``board token``'s own help
    promise the new link just works, and the 401 page tells the holder of a
    fresh link to go ask for a fresh link.
    """
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")          # phone holds a cookie
    fresh = bs.mint_token(pod["shared"], BOT)        # operator rotates

    r = c.get(f"/board/{BOT}?t={fresh}")             # the FIRST click
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/board/{BOT}")

    # The stale cookie is replaced, not merely cleared: both paths get the
    # fresh token, so the clean URL works immediately afterwards.
    cookies = r.headers.getlist("Set-Cookie")
    assert len(cookies) == 2
    assert all(fresh in c_ for c_ in cookies)
    assert c.get(f"/board/{BOT}").status_code == 200
    assert c.get(f"/api/board/{BOT}").status_code == 200


def test_a_stale_cookie_alone_is_refused_and_cleared(pod):
    """No fresh link presented: the rotated-away cookie is still a 401, and
    it is expired on the way out so it cannot shadow a later ``?t=``."""
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")
    bs.mint_token(pod["shared"], BOT)  # rotate: the held cookie is now stale
    r = c.get(f"/board/{BOT}")
    assert r.status_code == 401
    expired = r.headers.getlist("Set-Cookie")
    assert len(expired) == 2 and all("Expires=" in c_ for c_ in expired)


def test_a_wrong_query_token_over_a_stale_cookie_is_the_same_401(pod):
    """The second attempt adds no oracle: it can only turn a 401 into a
    success for a caller who presented a token that verifies. A wrong one
    lands on the single indistinguishable 401."""
    c = pod["client"]
    c.get(f"/api/board/{BOT}?t={pod['token']}")
    bs.mint_token(pod["shared"], BOT)  # rotate

    stale_and_wrong = c.get(f"/api/board/{BOT}?t=wrong")
    assert stale_and_wrong.status_code == 401

    fresh_client = pod["app"].test_client()
    baseline = fresh_client.get(f"/api/board/{BOT}?t=wrong")
    assert baseline.status_code == 401
    assert (stale_and_wrong.get_data(as_text=True)
            == baseline.get_data(as_text=True))


def test_revoke_kills_the_cookie_session(pod):
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")
    assert c.get(f"/api/board/{BOT}").status_code == 200
    assert bs.revoke_token(pod["shared"], BOT) is True
    assert c.get(f"/api/board/{BOT}").status_code == 401
    assert c.get(f"/api/board/{BOT}?t={pod['token']}").status_code == 401


# ── the 401 oracle is unchanged ────────────────────────────────────────────

def test_identical_401_for_every_failure_mode(pod):
    c = pod["client"]
    bodies = set()
    for url in (f"/api/board/{BOT}",
                f"/api/board/{BOT}?t=wrong",
                "/api/board/never-minted?t=wrong",
                "/api/board/no-such-bot?t=wrong"):
        r = c.get(url)
        assert r.status_code == 401
        bodies.add(r.get_data(as_text=True))
    assert len(bodies) == 1, "the failure mode must not be distinguishable"


def test_board_responses_carry_the_security_headers(pod):
    r = pod["client"].get(f"/api/board/{BOT}", headers=_bearer(pod))
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


# ── F-2: the body cap holds without a Content-Length ───────────────────────

def test_oversized_body_is_refused(pod):
    big = "x" * (32 * 1024)
    r = pod["client"].post(f"/api/board/{BOT}/cards", headers=_bearer(pod),
                           json={"title": big, "cluster": "admin"})
    assert r.status_code == 400


def test_chunked_body_cannot_bypass_the_cap(pod):
    """A chunked request carries no Content-Length; slice 1's
    ``content_length > cap`` test therefore never fired."""
    payload = json.dumps({"title": "x" * (32 * 1024), "cluster": "admin"})
    r = pod["client"].post(
        f"/api/board/{BOT}/cards",
        headers={**_bearer(pod), "Content-Type": "application/json",
                 "Transfer-Encoding": "chunked"},
        data=payload.encode("utf-8"),
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "JSON object body required"


def test_store_bounds_a_title_that_fits_the_body_cap(pod):
    r = pod["client"].post(f"/api/board/{BOT}/cards", headers=_bearer(pod),
                           json={"title": "y" * 900, "cluster": "admin"})
    assert r.status_code == 400
    assert "too long" in r.get_json()["error"]


# ── F-3: CSRF posture on cookie-authenticated writes ───────────────────────

def test_cross_origin_cookie_write_is_refused(pod):
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")  # cookie session
    r = c.post(f"/api/board/{BOT}/cards",
               headers={"Origin": "https://evil.example"},
               json={"title": "forged", "cluster": "admin"})
    # 403, not 429: a forged write must be told to stop, not to retry later.
    assert r.status_code == 403
    assert not any(x["title"] == "forged"
                   for x in bs.load_board(pod["shared"], BOT)["cards"])


def test_same_origin_cookie_write_is_allowed(pod):
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")
    r = c.post(f"/api/board/{BOT}/cards",
               headers={"Origin": "http://localhost"},
               json={"title": "from the phone", "cluster": "admin"})
    assert r.status_code == 201


def test_bearer_write_is_unaffected_by_origin(pod):
    """A bearer credential is not ambient — no third-party page can cause it
    to be sent, so the origin check does not apply to it."""
    r = pod["client"].post(f"/api/board/{BOT}/cards",
                           headers={**_bearer(pod),
                                    "Origin": "https://evil.example"},
                           json={"title": "tool write", "cluster": "admin"})
    assert r.status_code == 201


# ── F-4: rate limits ───────────────────────────────────────────────────────

def test_writes_are_rate_limited(pod):
    routes_board._write_limiter = RateLimiter(3, 60.0)
    c, seen = pod["client"], []
    for i in range(5):
        seen.append(c.post(f"/api/board/{BOT}/cards", headers=_bearer(pod),
                           json={"title": f"card {i}", "cluster": "admin"}
                           ).status_code)
    assert seen == [201, 201, 201, 429, 429]


def test_reads_are_not_charged_to_the_write_budget(pod):
    routes_board._write_limiter = RateLimiter(1, 60.0)
    c = pod["client"]
    for _ in range(5):
        assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 200
    assert c.post(f"/api/board/{BOT}/cards", headers=_bearer(pod),
                  json={"title": "one", "cluster": "admin"}).status_code == 201


def test_failed_auth_is_rate_limited_but_success_is_not_charged(pod):
    routes_board._auth_fail_limiter = RateLimiter(2, 60.0)
    c = pod["client"]
    # Successful requests must not consume the failure budget…
    for _ in range(5):
        assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 200
    # …while failures do, and an over-budget caller is refused identically
    # (a 401, not a distinguishable 429 — the limiter adds no oracle).
    for _ in range(2):
        assert c.get(f"/api/board/{BOT}?t=wrong").status_code == 401
    assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 401


def test_an_unreadable_token_store_is_not_charged_to_the_client(pod, monkeypatch):
    """The 2026-09-04 phone test, at the route.

    A root-owned ``token.sha256`` 401s every request no matter what token is
    presented. Charging those failures to the failed-auth limiter is the
    second silence: it then refuses the CORRECT token for the rest of the
    window, so repairing the store does not restore the link. The 401 itself
    is unchanged — the client still cannot tell an unreadable store from an
    unminted one.
    """
    routes_board._auth_fail_limiter = RateLimiter(2, 60.0)
    real_read_text = Path.read_text

    def _deny(self, *a, **kw):
        if self.name == "token.sha256":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _deny)
    bs._UNREADABLE_WARNED.clear()
    c = pod["client"]
    for _ in range(5):
        assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 401
    # Nothing was charged, so the store's repair is all it takes to recover.
    monkeypatch.undo()
    bs._UNREADABLE_WARNED.clear()
    assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 200


def test_an_unminted_board_still_charges_the_client(pod):
    """The other half: a board with no token IS the client's problem."""
    routes_board._auth_fail_limiter = RateLimiter(2, 60.0)
    c = pod["client"]
    for _ in range(2):
        assert c.get(f"/api/board/{BOT}?t=wrong").status_code == 401
    # Over budget now — even the good token is refused for the window.
    assert c.get(f"/api/board/{BOT}", headers=_bearer(pod)).status_code == 401


def test_rate_limiter_window_expires(pod):
    lim = RateLimiter(2, 10.0)
    assert lim.allow("k", now=100.0) is True
    assert lim.allow("k", now=100.5) is True
    assert lim.allow("k", now=101.0) is False
    assert lim.allow("k", now=111.5) is True


def test_rate_limiter_refusal_does_not_extend_the_penalty(pod):
    lim = RateLimiter(1, 10.0)
    assert lim.allow("k", now=100.0) is True
    for t in (101.0, 102.0, 109.0):
        assert lim.allow("k", now=t) is False
    assert lim.allow("k", now=110.5) is True


# ── add-to-home-screen ─────────────────────────────────────────────────────

def test_manifest_is_valid_json_scoped_to_this_board(pod):
    r = pod["client"].get(f"/board/{BOT}/manifest.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.content_type
    body = json.loads(r.get_data(as_text=True))
    assert body["start_url"] == f"/board/{BOT}"
    assert body["scope"] == f"/board/{BOT}"
    assert body["display"] == "standalone"
    assert {i["sizes"] for i in body["icons"]} == {"192x192", "512x512"}
    for icon in body["icons"]:
        assert pod["client"].get(icon["src"]).status_code == 200


def test_manifest_is_unauthenticated_and_is_not_an_oracle(pod):
    """A browser fetches the manifest without credentials, so gating it would
    break install. It must therefore reveal nothing: a syntactically valid
    bot id gets the same 200 whether or not that board exists."""
    minted = pod["client"].get(f"/board/{BOT}/manifest.webmanifest")
    unknown = pod["client"].get("/board/no-such-bot/manifest.webmanifest")
    assert minted.status_code == unknown.status_code == 200
    assert pod["client"].get("/board/NOT..VALID/manifest.webmanifest"
                             ).status_code == 404


def test_page_links_its_own_manifest_and_icons(pod):
    c = pod["client"]
    c.get(f"/board/{BOT}?t={pod['token']}")
    html = c.get(f"/board/{BOT}").get_data(as_text=True)
    assert 'rel="apple-touch-icon" href="/board/icon-192.png"' in html
    assert "/manifest.webmanifest" in html
    assert 'name="theme-color"' in html


# ── blast radius of a stolen token: verified, not asserted ─────────────────

def test_a_token_is_scoped_to_exactly_one_bot(pod):
    """The review's central claim about a stolen token — 'one bot's board,
    nothing else' — checked against the code rather than restated."""
    other = "team-bot-a"
    bs.save_board(pod["shared"], other, bs.load_board(pod["shared"], other))
    other_token = bs.mint_token(pod["shared"], other)
    c = pod["client"]

    # This bot's token opens this bot's board and no other, in both
    # directions, on read and on write.
    assert c.get(f"/api/board/{BOT}?t={pod['token']}").status_code == 200
    assert c.get(f"/api/board/{other}?t={pod['token']}").status_code == 401
    assert c.get(f"/api/board/{other}?t={other_token}").status_code == 200
    assert c.get(f"/api/board/{BOT}?t={other_token}").status_code == 401
    assert c.post(f"/api/board/{other}/cards?t={pod['token']}",
                  json={"title": "cross", "cluster": "admin"}).status_code == 401
    assert c.get(f"/board/{other}?t={pod['token']}").status_code == 401


def test_the_token_reaches_no_route_outside_the_board_namespace(pod):
    """Every rule this module registers lives under a board prefix. A route
    added here outside them would silently inherit the server.py exemption
    from the admin device gate — the module docstring's standing warning,
    made mechanical."""
    from evolve_admin.web.board_listener import is_board_path
    rules = [r.rule for r in pod["app"].url_map.iter_rules()
             if r.endpoint != "static"]
    assert rules, "the fixture registered no routes"
    for rule in rules:
        assert is_board_path(rule), rule
