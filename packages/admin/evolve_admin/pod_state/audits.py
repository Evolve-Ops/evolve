"""list_audits — read the latest OC security audit results.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``evolve_admin.audit_state`` — the in-memory cache the
admin server's background audit thread writes to (and the existing
``/api/security/audit`` route reads from).

v1 limits: the cache holds the **latest** audit per bot, not a
history. ``limit=N`` means "up to N bots' latest audits," not "N
historical audits." Spec wording is "recent audit results," which
matches: the cache itself is "recent." A historical view would
require persisting results to disk over time — out of scope for the
read-tool surface.

Staleness: if the cache hasn't been populated since the admin server
started (no background sweep has run yet), the response sets
``stale=true`` and returns an empty list. The bot can render "no
recent audit data — admin can hit refresh in the Security tab or
wait for the next scheduled sweep."
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

from .. import audit_state


DEFAULT_LIMIT = 5
MAX_LIMIT = 50


# How old is "fresh enough"? The background sweep takes ~30s for a
# small pod; after 6 hours the data is stale enough to warn about.
# This isn't a hard cutoff — we still return what's in the cache —
# but ``stale=true`` lets the bot signal "you might want to refresh."
FRESH_WINDOW_SECONDS = 6 * 60 * 60


def list_audits(
    shared_dir: Path,
    *,
    bot_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return the latest cached audit result(s).

    Parameters
    ----------
    shared_dir : Path
        Unused in v1 (the cache is in-process), but kept for signature
        parity with the other pod_state functions. Documented because a
        future disk-persisted variant would key off it.
    bot_id : str | None
        Filter to one bot's latest audit. ``None`` (default) returns
        the latest audit per bot, capped at ``limit`` entries.
    limit : int
        Cap on number of bots returned (when ``bot_id`` is None).
        Ignored when filtering to one bot. Default 5, max 50.

    Returns
    -------
    {
        "audits": [{"bot_id": str, "summary": {...}, "findings": [...],
                    ... (whatever _audit_run_one wrote)}, ...],
        "count": int,
        "cached_at": float | None,    # unix timestamp of last sweep
        "stale": bool,                # True if no sweep has populated
                                       # the cache, or last sweep is
                                       # > FRESH_WINDOW_SECONDS ago.
        "filters": {"bot_id": str | None, "limit": int},
    }
    """
    capped_limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))

    snap = audit_state.snapshot()
    cached_at = snap.get("cached_at")
    data = snap.get("data") or {}

    if not data:
        # No sweep has run since startup. The cache is genuinely empty —
        # not just "no findings." Distinguish in the response.
        return {
            "audits": [],
            "count": 0,
            "cached_at": cached_at,
            "stale": True,
            "known_bot": True,  # no data either way — caller shouldn't infer
            "filters": {"bot_id": bot_id, "limit": capped_limit},
        }

    # When the caller filters to one bot, distinguish "bot doesn't
    # exist in the cache at all" from "bot exists, no findings." The
    # HTTP route gates against network.json so this should be rare,
    # but a direct (MCP-bridge) caller can hit it — caught in second-
    # pass review of Bundle 3-part-3 as a silent-failure vector.
    known_bot = True
    audits: list[dict[str, Any]] = []
    if bot_id is not None:
        if bot_id in data:
            audits.append(_attach_bot_id(bot_id, data[bot_id]))
        else:
            known_bot = False
    else:
        # Latest audit per bot, in bot-id alphabetical order so the
        # output is deterministic across requests.
        for bid in sorted(data.keys()):
            audits.append(_attach_bot_id(bid, data[bid]))
            if len(audits) >= capped_limit:
                break

    # Future-dated cached_at (clock skew, NTP step, VM time warp)
    # would silently produce a negative delta and report stale=False
    # forever. Treat negative delta as stale too.
    if cached_at is None:
        stale = True
    else:
        now = _time.time()
        delta = now - float(cached_at)
        stale = (delta < 0) or (delta > FRESH_WINDOW_SECONDS)

    return {
        "audits": audits,
        "count": len(audits),
        "cached_at": cached_at,
        "stale": stale,
        "known_bot": known_bot,
        "filters": {"bot_id": bot_id, "limit": capped_limit},
    }


def _attach_bot_id(bot_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Stamp the bot_id onto a result dict for return-shape consistency.

    The cache keys by bot_id but the values don't include it; the bot
    rendering the response should not have to walk a parent mapping
    to figure out which bot a row is about.

    Shallow copy is safe here ONLY because ``audit_state.snapshot()``
    already deep-copies. If that ever flips back to shallow, this
    function would let nested mutations leak into the live cache.
    """
    out = dict(result or {})
    out["bot_id"] = bot_id
    return out
