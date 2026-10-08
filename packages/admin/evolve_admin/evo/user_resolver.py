"""Background warm of a render-ready *user*-name cache for the Usage page.

The user-name twin of :mod:`evolve_admin.evo.conversation_resolver`. Where
that module turns a **conversation** id into a label and caches it under
``pod.conversations.resolved``, this one turns a messaging-platform **user**
id into a display name and caches it under ``pod.users.resolved`` — the
conversation analogue's sibling.

Why a sibling cache at all? :mod:`name_resolver` already resolves users and
writes positives into ``pod.admins.resolved_names`` (the cache
:func:`evolve_admin.roster_resolver.resolve_display_name` reads first). But:

  * That cache is keyed/named for *admins*; the non-admin people who show up
    on the pod-wide Usage "By User" report (a Slack workspace member who DM'd
    a bot but was never admitted) never land there because nothing resolves
    them on a cadence the Usage render can rely on.
  * ``name_resolver`` has **no negative cache**, so every warm of an
    unresolvable id (a deactivated account, a foreign-workspace id, a bot
    pseudo-user) re-hits the live Slack/Telegram API forever.

This module fixes both with the exact ``cached_* / write_* / resolve_* /
enrich_* / warm_*`` shape ``conversation_resolver`` established (PR #2990):
the **render reads the cache only**; the cache is warmed in the background by
:func:`warm_user_cache` (a sweep entrypoint reusing
:func:`evolve_admin.evo.identity_discovery.discover_candidates` — the *exact*
path the Users page uses) and :func:`enrich_user_names` (a time-boxed
best-effort populate). Unresolvable ids are written as a **negative
sentinel** (``resolved: false``) so a user the bot can't name isn't re-hit
on every sweep.

Reuse, don't reinvent: the platform HTTP primitives and per-platform user
lookups (``_resolve_slack`` / ``_resolve_telegram`` / ``_resolve_discord``),
plus the workspace-scoped token picker (``_channel_token``), all come from
``name_resolver``. The only thing new here is the user cache (positive +
negative) and the pod-wide warm sweep.

Cache contract (``roster_resolver.resolve_display_name`` binds to this — see
``internal/spec-users-meta-2026-06-15.md`` §"User-name cache contract"):

    key:   "<platform>:<user_id>"   # platform lower-cased; user_id verbatim
    value (positive):
        {"resolved": true,
         "name":     "<best-effort human display name>|null",
         "username": "<platform @handle>|null",
         "email":    "<email>"      # Slack only, when users:read.email scope
         "cached_at": "<ISO-8601 Z>"}
    value (negative / unresolvable):
        {"resolved": false, "name": null, "username": null,
         "cached_at": "<ISO-8601 Z>"}

A consumer reads ``cached_user(...)`` (cache-only) and uses ``entry["name"]``
(then ``entry["username"]``) when the entry is positive, falling back to the
raw id otherwise — a negative or absent entry both yield the raw-id fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import name_resolver as nr

log = logging.getLogger(__name__)


# ─── Platforms we know how to resolve users for ─────────────────────────────
#
# Identical to ``name_resolver.SUPPORTED_CHANNELS`` (slack/telegram/discord) —
# this module is a thin warm layer over that resolver, so it can name exactly
# the platforms it can. Kept as a reference (not a copy) so the two can't drift.
SUPPORTED_USER_PLATFORMS: frozenset[str] = nr.SUPPORTED_CHANNELS

DEFAULT_CACHE_TTL_DAYS: int = nr.DEFAULT_CACHE_TTL_DAYS


def _cache_key(platform: str, user_id: str) -> str:
    """``"<platform>:<user_id>"`` — platform lower-cased, user_id verbatim.

    Parallel to ``name_resolver._cache_key`` and
    ``conversation_resolver._cache_key`` (which lower-case only the platform,
    not the external id).
    """
    return f"{platform.lower()}:{user_id}"


# ─── Cache read / write ─────────────────────────────────────────────────────


def cached_user(
    network: dict[str, Any], platform: str, user_id: str,
    max_age_days: int = DEFAULT_CACHE_TTL_DAYS,
) -> dict | None:
    """Return the cached entry for a user, without an API call.

    Returns the entry whether it is **positive** (``resolved: true`` with a
    ``name``/``username``) or **negative** (``resolved: false`` sentinel) —
    callers distinguish on ``entry["resolved"]``. Returns None when there is
    no entry or it is older than ``max_age_days`` (a stale entry, positive or
    negative, re-resolves on the next ``resolve_user`` call). Pass
    ``max_age_days=0`` to ignore freshness.
    """
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return None
    users = pod.get("users") or {}
    if not isinstance(users, dict):
        return None
    cache = users.get("resolved") or {}
    if not isinstance(cache, dict):
        return None
    entry = cache.get(_cache_key(platform, user_id))
    if not isinstance(entry, dict):
        return None
    if nr._entry_is_stale(entry, max_age_days):
        return None
    return entry


def write_user_cache(
    network: dict[str, Any], platform: str, user_id: str,
    resolved: "dict | None",
) -> None:
    """Persist a resolution (or a negative sentinel) into ``network``.

    ``resolved`` is the ``{username, name, email?}`` dict on success, or None
    to write the negative sentinel. Caller is responsible for ``save_network``
    afterward.
    """
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    users = pod.setdefault("users", {})
    if not isinstance(users, dict):
        users = {}
        pod["users"] = users
    cache = users.setdefault("resolved", {})
    if not isinstance(cache, dict):
        cache = {}
        users["resolved"] = cache

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")

    if resolved is None:
        entry: dict[str, Any] = {
            "resolved": False,
            "name": None,
            "username": None,
            "cached_at": now,
        }
    else:
        entry = {
            "resolved": True,
            "name": resolved.get("name"),
            "username": resolved.get("username"),
            "cached_at": now,
        }
        # ``email`` only when the platform exposes it (Slack with
        # users:read.email scope) — keep the key absent otherwise so the
        # JSON stays small, matching name_resolver.write_cache_entry.
        if resolved.get("email"):
            entry["email"] = resolved["email"]

    cache[_cache_key(platform, user_id)] = entry


# ─── Public entry point ─────────────────────────────────────────────────────


def resolve_user(
    network: dict[str, Any], platform: str, user_id: str,
    *, bot_id: "str | None" = None, use_cache: bool = True,
) -> dict | None:
    """Resolve a user id to the positive cache-shaped dict, or None.

    Strategy (identical shape to ``conversation_resolver.resolve_conversation``):
      1. If ``use_cache`` and a cache entry exists — positive returns the
         cached dict; **negative returns None without any API call** (the
         negative sentinel is what stops us re-hitting unresolvable ids).
      2. Pick the right bot token (workspace-scoped — pass ``bot_id`` so a
         cross-workspace Slack bot resolves against its own token).
      3. Call the platform user API (reusing ``name_resolver``'s lookups).
      4. Write the result (positive *or* negative) into the cache; caller
         persists.

    Returns the positive cache-shaped dict
    (``{resolved, name, username, cached_at, email?}``) on success, or None
    when:
      - the platform isn't supported (WhatsApp / unknown) — no cache write;
      - the user_id is empty — no cache write;
      - no bot has the platform configured — no cache write (transient: the
        token may appear later);
      - the user can't be resolved — a **negative** entry is written so the
        next sweep skips it.
    """
    platform = (platform or "").lower()
    user_id = (user_id or "").strip()
    if platform not in SUPPORTED_USER_PLATFORMS or not user_id:
        return None

    if use_cache:
        entry = cached_user(network, platform, user_id)
        if entry is not None:
            return entry if entry.get("resolved") else None

    token = nr._channel_token(network, platform, prefer_bot_id=bot_id)
    if not token:
        # No token to even attempt — transient; don't poison the cache.
        return None

    if platform == "slack":
        resolved = nr._resolve_slack(token, user_id)
    elif platform == "telegram":
        resolved = nr._resolve_telegram(token, user_id)
    elif platform == "discord":
        resolved = nr._resolve_discord(token, user_id)
    else:  # pragma: no cover - guarded by SUPPORTED_USER_PLATFORMS
        return None

    write_user_cache(network, platform, user_id, resolved)
    if resolved is None:
        return None
    return cached_user(network, platform, user_id, max_age_days=0)


# ─── Background populate (render reads cache ONLY) ──────────────────────────


def enrich_user_names(
    network: dict[str, Any], pairs: "Iterable[tuple[str, str]]",
    *, bot_id: "str | None" = None, max_seconds: float = 5.0,
) -> bool:
    """Time-boxed best-effort resolve of a batch of ``(platform, user_id)`` pairs.

    Mirrors ``conversation_resolver.enrich_conversation_names``: builds a work
    list of pairs that aren't already freshly cached, fires concurrent
    ``resolve_user(use_cache=False)`` calls under a wall-clock budget, mutates
    the cache in place, and returns True iff at least one pair resolved
    (positive **or** negative — both write the cache, so the caller should
    ``save_network`` either way). Stragglers past the budget keep running and
    their cache writes land for the next reader.

    Pairs whose platform isn't a supported user platform are skipped.

    NEVER call this from a render path — it makes live Slack/Telegram/Discord
    calls.
    """
    import time
    from concurrent.futures import (
        ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed,
    )

    # De-dupe + normalize + drop already-fresh + drop unsupported platforms.
    work: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_platform, raw_id in pairs:
        platform = (raw_platform or "").lower()
        user_id = (raw_id or "").strip()
        if platform not in SUPPORTED_USER_PLATFORMS or not user_id:
            continue
        key = (platform, user_id)
        if key in seen:
            continue
        seen.add(key)
        if cached_user(network, platform, user_id) is not None:
            continue  # already fresh (positive or negative) — skip the call
        work.append(key)

    if not work:
        return False

    any_resolved = False
    deadline = time.monotonic() + max_seconds
    ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="user-enrich")
    try:
        futures = {
            ex.submit(
                resolve_user, network, platform, user_id,
                bot_id=bot_id, use_cache=False,
            ): (platform, user_id)
            for (platform, user_id) in work
        }
        try:
            for fut in as_completed(
                futures, timeout=max(0.1, deadline - time.monotonic()),
            ):
                platform, user_id = futures[fut]
                try:
                    fut.result(timeout=0)
                except Exception:  # noqa: BLE001
                    continue
                # Count a change iff this run wrote a FRESH entry (positive OR
                # negative sentinel). The work list was pre-filtered to
                # absent/stale ids, so a fresh entry now means this run wrote
                # it. The no-token path writes nothing and leaves any stale
                # entry stale, so it is correctly not counted.
                if cached_user(network, platform, user_id) is not None:
                    any_resolved = True
        except FuturesTimeout:
            log.debug(
                "user enrichment hit budget %ss with stragglers", max_seconds,
            )
    finally:
        ex.shutdown(wait=False)
    return any_resolved


def warm_user_cache(
    network: dict[str, Any], *,
    bot_id: "str | None" = None, days: int = 30, max_seconds: float = 20.0,
) -> bool:
    """Sweep entrypoint: warm the user-name cache from recent turn history.

    For each bot in the pod, runs ``identity_discovery.discover_candidates``
    (the *exact* discovery the Users page uses) + the Slack D-channel→U-user
    rewrite, then enriches each bot's ``(platform, user_id)`` pairs with that
    bot's own token (workspace-scoped — critical for Slack). Returns True iff
    any id resolved, so the caller can ``save_network``.

    Independent of any page load — schedulable as a daily cron, like
    ``conversation_resolver.warm_conversation_cache``. Render paths must NOT
    call this.

    ``days`` defaults to 30 (the Users page's lookback) rather than the Usage
    page's 7, so the warm covers everyone the operator can see on Users — a
    superset of the 7-day By-User report.
    """
    from . import identity_discovery as idsc

    bots = list((network.get("bots") or {}).keys())
    if bot_id and bot_id not in bots:
        bots.append(bot_id)
    if not bots:
        return False

    # bot_id → [(platform, user_id)] so each group is enriched with the bot
    # that actually served those users (its token is workspace-scoped).
    groups: dict[str, list[tuple[str, str]]] = {}
    for b in bots:
        bot_user = (
            (network.get("bots") or {}).get(b) or {}
        ).get("user") or b
        try:
            cands = idsc.discover_candidates(
                b, bot_user=bot_user, lookback_days=days, top_k=50)
            # D-channel→U-user rewrite: the same normalization the Users page
            # applies so we resolve (and cache) the actual user, not the IM
            # channel id.
            cands = idsc._rewrite_slack_dm_candidates(
                network, cands, bot_id=b)
        except Exception as exc:  # noqa: BLE001 - best-effort discovery
            log.debug("user warm discovery for %s failed: %s", b, exc)
            continue
        pairs = [
            (c.get("channel") or "", c.get("external_id") or "")
            for c in cands
            if c.get("channel") and c.get("external_id")
        ]
        if pairs:
            groups[b] = pairs

    if not groups:
        return False

    # Split the wall-clock budget across bot groups so the whole sweep stays
    # inside ``max_seconds``; a hard global deadline backstops the sum. Mirrors
    # conversation_resolver.warm_conversation_cache.
    import time

    per_group = max_seconds / max(1, len(groups))
    deadline = time.monotonic() + max_seconds
    any_resolved = False
    for grp_bot, pairs in groups.items():
        if time.monotonic() >= deadline:
            log.debug("user sweep hit %ss budget; %d group(s) deferred",
                      max_seconds, len(groups))
            break
        if enrich_user_names(
            network, pairs, bot_id=grp_bot or bot_id, max_seconds=per_group,
        ):
            any_resolved = True
    return any_resolved


# ─── CLI sweep entrypoint ───────────────────────────────────────────────────


def main(argv: "list[str] | None" = None) -> int:
    """``python3 -m evolve_admin.evo.user_resolver`` — warm the user cache.

    Loads ``network.json``, warms the user-name cache from recent turns, and
    persists if anything changed. Idempotent; safe to schedule daily.
    """
    import argparse

    from ..config import DEFAULT_NETWORK_CONFIG, load_network, save_network

    parser = argparse.ArgumentParser(
        description="Warm the user-name cache from recent turns.",
    )
    parser.add_argument(
        "--network-path", default=str(DEFAULT_NETWORK_CONFIG),
        help="Path to network.json (default: the canonical pod config).",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="How many days of turn history to scan (default: 30).",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=20.0,
        help="Wall-clock budget for the whole sweep (default: 20).",
    )
    args = parser.parse_args(argv)

    from pathlib import Path
    net_path = Path(args.network_path)
    network = load_network(net_path)
    changed = warm_user_cache(
        network, days=args.days, max_seconds=args.max_seconds,
    )
    if changed:
        save_network(network, net_path)
        log.info("user cache warmed; network.json updated")
    else:
        log.info("user cache: nothing new to resolve")
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
