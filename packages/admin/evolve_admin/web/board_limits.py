"""board_limits.py — request-size and request-rate bounds for the board API.

Findings: ``internal/review-board-web-surface-2026-09.md`` F-2 (unbounded
body on a chunked request) and F-4 (no rate limit on a write surface).

Two small, boring mechanisms:

``read_bounded_json``
    Slice 1 bounded write bodies with ``request.content_length > cap``. A
    request sent with ``Transfer-Encoding: chunked`` has **no**
    ``Content-Length``, so ``(None or 0) > cap`` was False and the body was
    then parsed in full — the cap was advisory, not enforced. This reads at
    most ``cap + 1`` bytes off the stream and refuses anything longer, which
    holds for both framings.

``RateLimiter``
    A fixed-cost sliding window, per key, in memory. Deliberately NOT a
    distributed or persisted limiter: the admin daemon is one process per
    pod, and the thing being bounded is a single phone's thumb plus the blast
    radius of one stolen board token — not a botnet. Two limiters are wired
    up in ``routes_board``: writes per bot, and failed authentications per
    client address (which turns a token-guessing loop from "free" into
    "visibly rate-limited", on top of the 256-bit token that already makes
    guessing hopeless).

Both limiters are process-local and reset on daemon restart. That is stated
rather than hidden: a restart-driven reset is fine for a thumb-speed bound,
and the durable control on a stolen token is ``evolve-admin board revoke``.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Deque

from flask import request

#: Mutation bodies are a title, a note and a lane. Anything larger is a
#: client bug or an attempt to grow the store through the API.
MAX_WRITE_BODY = 16 * 1024

#: Writes per bot per window. A fast human tapping cards moves maybe one
#: card a second; 120/min leaves two orders of magnitude of headroom over
#: real use while still bounding a runaway client or a stolen token.
WRITE_LIMIT = 120
WRITE_WINDOW_SECONDS = 60.0

#: Failed authentications per client address per window.
AUTH_FAIL_LIMIT = 60
AUTH_FAIL_WINDOW_SECONDS = 60.0


def read_bounded_json(cap: int = MAX_WRITE_BODY) -> dict[str, Any] | None:
    """Parse this request's JSON object body, refusing anything over ``cap``.

    Returns ``None`` for every rejection — too large, unparseable, or not a
    JSON object — so the caller answers with one 400 shape and leaks nothing
    about which it was.
    """
    length = request.content_length
    if length is not None and length > cap:
        return None
    try:
        raw = request.stream.read(cap + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > cap:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


class RateLimiter:
    """Sliding-window counter: at most ``limit`` hits per ``window`` per key."""

    #: Sweep idle keys once the table passes this size, so a long-lived
    #: daemon does not retain one entry per client address ever seen.
    _SWEEP_AT = 512

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record a hit for ``key`` and report whether it is within budget.

        A refused hit is NOT recorded — otherwise a caller that keeps
        hammering while blocked would extend its own penalty indefinitely,
        which turns a rate limit into a lockout.
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._prune(key, stamp)
            if len(bucket) >= self._limit:
                return False
            bucket.append(stamp)
            return True

    def over_budget(self, key: str, *, now: float | None = None) -> bool:
        """Is ``key`` already at its limit? Read-only — records nothing.

        Paired with :meth:`record` where only SOME outcomes should count
        against the budget (the failed-auth limiter charges failures, not
        the successful requests that share the same code path).
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, stamp)) >= self._limit

    def record(self, key: str, *, now: float | None = None) -> None:
        """Charge one hit to ``key`` regardless of budget."""
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._prune(key, stamp).append(stamp)

    def _prune(self, key: str, stamp: float) -> Deque[float]:
        """Expire ``key``'s out-of-window hits and return its bucket.
        Caller holds the lock."""
        cutoff = stamp - self._window
        if len(self._hits) >= self._SWEEP_AT:
            self._sweep(cutoff)
        bucket = self._hits.setdefault(key, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def _sweep(self, cutoff: float) -> None:
        """Drop keys whose whole window has expired. Caller holds the lock."""
        for k in [k for k, b in self._hits.items() if not b or b[-1] <= cutoff]:
            del self._hits[k]


def client_key() -> str:
    """A stable-enough identity for the failed-auth limiter.

    ``remote_addr`` only — the board listener is bound to a tailnet address
    with no proxy in front, so ``X-Forwarded-For`` here would be attacker-
    controlled input, not a fact. Trusting it would let one client mint
    unlimited identities and erase the limit.
    """
    return request.remote_addr or "unknown"
