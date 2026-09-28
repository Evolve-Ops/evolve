"""Resolve a messaging-platform *conversation* id to a human-readable label.

The sibling of :mod:`evolve_admin.evo.name_resolver`. Where that module
turns a **user** id into a name (``U0PLKKXV0`` → "Pod_admin Alden"), this
one turns a **conversation** id into a render-ready label:

  * Slack public/private channel ``C7T9FBY6S`` → ``"#product-team"``
  * Slack 1:1 DM ``D0AKX41HELU``            → ``"DM · Alice"``
  * Slack group DM (mpim)                    → ``"Group DM"``
  * Telegram group / supergroup / channel    → the chat ``title``
  * Telegram private chat                    → ``"DM · Bob"``

Operators see raw conversation ids on the Usage "By Channel" table and the
Users page. This module is the **data layer** that lets a later UI pass
(model-tiers Bite 3, ``usage-channel-names``) render names instead. It
mirrors ``name_resolver`` exactly: cache-then-API-then-write, the caller
persists with ``save_network``, and **no live API is ever called from a
render path** — the render reads the cache only; the cache is warmed in
the background by :func:`warm_conversation_cache` (a sweep entrypoint) and
:func:`enrich_conversation_names` (a time-boxed best-effort populate).

Resolved labels are cached in ``network.json`` under
``pod.conversations.resolved[<platform>:<conv_id>]`` — the conversation
analogue of ``pod.admins.resolved_names``. Unresolvable ids are cached as
a **negative sentinel** (``resolved: false``) so a channel the bot can't
see isn't re-hit on every sweep.

Reuse, don't reinvent: the platform HTTP primitives (``_http_get_json``),
the workspace-scoped token picker (``_channel_token``), and the user-name
resolver (``resolve``) all come from ``name_resolver``. The only thing new
here is the *conversation*-shaped API calls (Slack ``conversations.info``,
Telegram ``getChat`` for a group title) and the conversation cache.

Cache contract (model-tiers Bite 3 binds to this — see
``internal/spec-users-meta-2026-06-15.md`` §"Conversation-name cache
contract"):

    key:   "<platform>:<conv_id>"   # platform lower-cased; conv_id is the
                                     # base id with any ":thread:<ts>"
                                     # suffix stripped, kept verbatim
    value (positive):
        {"resolved": true,
         "kind": "channel|private|group_dm|dm|telegram_group",
         "name":  "<raw channel name / group title / DM peer name>|null",
         "label": "<render-ready, e.g. '#product-team' / 'DM · Alice'>",
         "members": ["U…", …],   # optional, group DM only, when cheap
         "cached_at": "<ISO-8601 Z>"}
    value (negative / unresolvable):
        {"resolved": false, "kind": null, "name": null, "label": null,
         "cached_at": "<ISO-8601 Z>"}

A consumer reads ``cached_conversation(...)`` (cache-only) and uses
``entry["label"]`` when truthy, falling back to the raw id otherwise — a
negative or absent entry both yield the raw-id fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import name_resolver as nr

log = logging.getLogger(__name__)


# ─── Platforms we know how to resolve conversations for ─────────────────────
#
# A subset of ``name_resolver.SUPPORTED_CHANNELS`` — conversation metadata is
# only wired for Slack + Telegram (Discord channel resolution is a later add;
# its ids fall through to None, no negative cache, so a future implementation
# can populate them cleanly). Kept provider-neutral: control flow branches on
# membership, not on literals scattered through the code.
SUPPORTED_CONVERSATION_PLATFORMS: frozenset[str] = frozenset({"slack", "telegram"})

# Mirror of ``usage_analytics._THREAD_MARKER`` / ``_split_thread``: a turn's
# channel for a Slack thread is ``<parent>:thread:<ts>``. Conversations roll
# up to their parent, so the cache is keyed by the parent id. Defined locally
# to keep this module import-clean of the analyzer package on the core path.
_THREAD_MARKER = ":thread:"

# Telegram chat ``type`` values that denote a multi-party conversation whose
# ``title`` is the right label (vs. ``private`` → a 1:1 DM).
_TELEGRAM_GROUP_TYPES: frozenset[str] = frozenset(
    {"group", "supergroup", "channel"},
)

DEFAULT_CACHE_TTL_DAYS: int = nr.DEFAULT_CACHE_TTL_DAYS


# ─── id normalization ───────────────────────────────────────────────────────


def normalize_conv_id(conv_id: str) -> str:
    """Strip any ``:thread:<ts>`` suffix → the base conversation id.

    Mirrors ``usage_analytics._split_thread``'s parent extraction so a
    threaded turn (``c7t9fby6s:thread:1700000000.001``) and its parent
    conversation share one cache entry. Returns the id unchanged when there
    is no thread marker. Case is preserved — the key uses the id verbatim so
    producers (the sweep + Bite 3) that both source the id from the same turn
    ``channel`` field stay consistent.
    """
    s = (conv_id or "").strip()
    idx = s.find(_THREAD_MARKER)
    return s[:idx] if idx >= 0 else s


def _cache_key(platform: str, conv_id: str) -> str:
    """``"<platform>:<conv_id>"`` — platform lower-cased, conv_id verbatim.

    Parallel to ``name_resolver._cache_key`` (which lower-cases only the
    channel, not the external id).
    """
    return f"{platform.lower()}:{conv_id}"


# ─── Slack conversation lookup ──────────────────────────────────────────────


def _slack_conversation_info(token: str, conv_id: str) -> dict | None:
    """Call Slack ``conversations.info`` and return the ``channel`` object.

    Uses the same GET + ``Authorization: Bearer`` shape as
    ``name_resolver.slack_im_channel_to_user`` (and the same mockable
    ``name_resolver._http_get_json`` primitive), rather than the POST-based
    ``integrations.slack.slack_client.SlackClient.conversations_info`` — both
    return the identical channel record; the GET keeps this module's HTTP on
    one patchable surface for tests.

    Slack object ids are canonically upper-case (``C…``/``D…``/``G…``); the
    lower-cased stems that appear in OC session keys (``c7t9fby6s``) are
    up-cased for the API call only. The cache key keeps the original casing.
    """
    api_id = conv_id.upper()
    url = f"https://slack.com/api/conversations.info?channel={api_id}"
    payload, err = nr._http_get_json(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    if payload is None:
        log.debug("slack conversations.info failed for %s: %s", conv_id, err)
        return None
    if not payload.get("ok"):
        log.debug(
            "slack conversations.info not-ok for %s: %s",
            conv_id, payload.get("error"),
        )
        return None
    ch = payload.get("channel")
    return ch if isinstance(ch, dict) else None


def _resolve_slack_conversation(
    network: dict[str, Any], conv_id: str, token: str,
    *, bot_id: "str | None",
) -> dict | None:
    """Resolve a Slack conversation id → ``{kind, name, label, members?}``.

    Returns None when the channel can't be classified (caller writes a
    negative cache entry). DM peer-name resolution delegates to
    ``name_resolver.resolve`` (the user-name half) so we don't duplicate
    ``users.info`` handling.
    """
    ch = _slack_conversation_info(token, conv_id)
    if ch is None:
        return None

    # 1:1 DM — conversations.info carries the peer ``user`` directly.
    if ch.get("is_im"):
        peer = ch.get("user")
        name: "str | None" = None
        if isinstance(peer, str) and peer:
            resolved = nr.resolve(
                network, channel="slack", external_id=peer,
                use_cache=True, bot_id=bot_id,
            )
            if resolved:
                name = resolved.get("name")
        return {
            "kind": "dm",
            "name": name,
            "label": f"DM · {name}" if name else "Direct message",
        }

    # Group DM (multi-party IM). Member names would cost an extra
    # conversations.members + per-user resolve — not cheap, so we stop at the
    # generic label and leave ``members`` absent.
    if ch.get("is_mpim"):
        return {"kind": "group_dm", "name": None, "label": "Group DM"}

    # Public channel / private channel (group). Both carry a ``name``.
    name = ch.get("name")
    if isinstance(name, str) and name:
        kind = "private" if ch.get("is_private") else "channel"
        return {"kind": kind, "name": name, "label": f"#{name}"}

    # Unknown Slack conversation shape → unresolvable.
    return None


# ─── Telegram conversation lookup ───────────────────────────────────────────


def _resolve_telegram_conversation(
    network: dict[str, Any], conv_id: str, token: str,
) -> dict | None:
    """Resolve a Telegram chat id → ``{kind, name, label}``.

    ``getChat`` returns ``title`` for a group/supergroup/channel and
    ``first_name``/``last_name``/``username`` for a private chat. We branch on
    ``type`` so a group keeps its title (which ``name_resolver._resolve_telegram``
    would drop, since it only builds a person-name).
    """
    url = (
        f"https://api.telegram.org/bot{token}/getChat?chat_id={conv_id}"
    )
    payload, err = nr._http_get_json(url)
    if payload is None:
        log.debug("telegram getChat failed for %s: %s", conv_id, err)
        return None
    if not payload.get("ok"):
        log.debug(
            "telegram getChat not-ok for %s: %s",
            conv_id, payload.get("description"),
        )
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    chat_type = (result.get("type") or "").lower()
    if chat_type in _TELEGRAM_GROUP_TYPES:
        title = result.get("title")
        if isinstance(title, str) and title.strip():
            return {
                "kind": "telegram_group",
                "name": title.strip(),
                "label": title.strip(),
            }
        return None

    if chat_type == "private":
        username = result.get("username") or None
        first = result.get("first_name") or ""
        last = result.get("last_name") or ""
        name = (first + " " + last).strip() or username or None
        return {
            "kind": "dm",
            "name": name,
            "label": f"DM · {name}" if name else "Direct message",
        }

    return None


# ─── Cache read / write ─────────────────────────────────────────────────────


def cached_conversation(
    network: dict[str, Any], platform: str, conv_id: str,
    max_age_days: int = DEFAULT_CACHE_TTL_DAYS,
) -> dict | None:
    """Return the cached entry for a conversation, without an API call.

    Returns the entry whether it is **positive** (``resolved: true`` with a
    ``label``) or **negative** (``resolved: false`` sentinel) — callers
    distinguish on ``entry["resolved"]`` / ``entry["label"]``. Returns None
    when there is no entry or it is older than ``max_age_days`` (a stale
    entry, positive or negative, re-resolves on the next ``resolve`` call).
    Pass ``max_age_days=0`` to ignore freshness.

    The conv_id is normalized (thread-stripped) before lookup so a threaded
    id hits the parent's entry.
    """
    pod = network.get("pod") or {}
    if not isinstance(pod, dict):
        return None
    conversations = pod.get("conversations") or {}
    if not isinstance(conversations, dict):
        return None
    cache = conversations.get("resolved") or {}
    if not isinstance(cache, dict):
        return None
    entry = cache.get(_cache_key(platform, normalize_conv_id(conv_id)))
    if not isinstance(entry, dict):
        return None
    if nr._entry_is_stale(entry, max_age_days):
        return None
    return entry


def write_conversation_cache(
    network: dict[str, Any], platform: str, conv_id: str,
    resolved: "dict | None",
) -> None:
    """Persist a resolution (or a negative sentinel) into ``network``.

    ``resolved`` is the ``{kind, name, label, members?}`` dict on success, or
    None to write the negative sentinel. Caller is responsible for
    ``save_network`` afterward. The conv_id is normalized (thread-stripped)
    so the key matches what a render-time reader passes.
    """
    pod = network.setdefault("pod", {})
    if not isinstance(pod, dict):
        pod = {}
        network["pod"] = pod
    conversations = pod.setdefault("conversations", {})
    if not isinstance(conversations, dict):
        conversations = {}
        pod["conversations"] = conversations
    cache = conversations.setdefault("resolved", {})
    if not isinstance(cache, dict):
        cache = {}
        conversations["resolved"] = cache

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")

    if resolved is None:
        entry: dict[str, Any] = {
            "resolved": False,
            "kind": None,
            "name": None,
            "label": None,
            "cached_at": now,
        }
    else:
        entry = {
            "resolved": True,
            "kind": resolved.get("kind"),
            "name": resolved.get("name"),
            "label": resolved.get("label"),
            "cached_at": now,
        }
        # ``members`` only when cheaply available (group DM); keep absent
        # otherwise so the JSON stays small.
        if resolved.get("members"):
            entry["members"] = list(resolved["members"])

    cache[_cache_key(platform, normalize_conv_id(conv_id))] = entry


# ─── Public entry point ─────────────────────────────────────────────────────


def resolve_conversation(
    network: dict[str, Any], platform: str, conv_id: str,
    *, bot_id: "str | None" = None, use_cache: bool = True,
) -> dict | None:
    """Resolve a conversation id to a render-ready label dict.

    Strategy (identical shape to ``name_resolver.resolve``):
      1. If ``use_cache`` and a cache entry exists — positive returns the
         cached dict; **negative returns None without any API call** (the
         negative sentinel is what stops us re-hitting unresolvable ids).
      2. Pick the right bot token (workspace-scoped — pass ``bot_id`` so a
         cross-workspace Slack bot resolves against its own token).
      3. Call the platform conversation API.
      4. Write the result (positive *or* negative) into the cache; caller
         persists.

    Returns the positive cache-shaped dict
    (``{resolved, kind, name, label, cached_at, members?}``) on success, or
    None when:
      - the platform isn't supported (Discord / unknown) — no cache write;
      - the conv_id is empty — no cache write;
      - no bot has the channel configured — no cache write (transient: the
        token may appear later);
      - the conversation can't be classified — a **negative** entry is
        written so the next sweep skips it.
    """
    platform = (platform or "").lower()
    conv_id = normalize_conv_id(conv_id)
    if platform not in SUPPORTED_CONVERSATION_PLATFORMS or not conv_id:
        return None

    if use_cache:
        entry = cached_conversation(network, platform, conv_id)
        if entry is not None:
            return entry if entry.get("resolved") else None

    token = nr._channel_token(network, platform, prefer_bot_id=bot_id)
    if not token:
        # No token to even attempt — transient; don't poison the cache.
        return None

    if platform == "slack":
        resolved = _resolve_slack_conversation(
            network, conv_id, token, bot_id=bot_id,
        )
    elif platform == "telegram":
        resolved = _resolve_telegram_conversation(network, conv_id, token)
    else:  # pragma: no cover - guarded by SUPPORTED_CONVERSATION_PLATFORMS
        return None

    write_conversation_cache(network, platform, conv_id, resolved)
    if resolved is None:
        return None
    return cached_conversation(network, platform, conv_id, max_age_days=0)


# ─── Background populate (render reads cache ONLY) ──────────────────────────


def enrich_conversation_names(
    network: dict[str, Any], conv_ids: Iterable[str],
    *, bot_id: "str | None" = None, max_seconds: float = 5.0,
) -> bool:
    """Time-boxed best-effort resolve of a batch of conversation ids.

    Mirrors ``routes_bot_users._enrich_unknown_names``: builds a work list of
    ids that aren't already freshly cached, fires concurrent
    ``resolve_conversation(use_cache=False)`` calls under a wall-clock budget,
    mutates the cache in place, and returns True iff at least one id resolved
    (positive **or** negative — both write the cache, so the caller should
    ``save_network`` either way). Stragglers past the budget keep running and
    their cache writes land for the next reader.

    ``conv_ids`` are raw conversation-id strings (e.g. from turn rollups); the
    platform is inferred per id via ``usage_analytics._infer_platform``
    (lazy-imported — this is a background path, not a render path). Ids whose
    platform isn't a supported conversation platform are skipped.

    NEVER call this from a render path — it makes live Slack/Telegram calls.
    """
    import time
    from concurrent.futures import (
        ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed,
    )

    try:
        from usage_analytics import _infer_platform
    except Exception:  # noqa: BLE001 - analyzer unavailable → nothing to do
        log.debug("usage_analytics._infer_platform unavailable; skip enrich")
        return False

    # De-dupe + normalize + drop already-fresh + drop unsupported platforms.
    work: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in conv_ids:
        conv_id = normalize_conv_id(raw or "")
        if not conv_id:
            continue
        platform = _infer_platform(conv_id)
        if platform not in SUPPORTED_CONVERSATION_PLATFORMS:
            continue
        key = (platform, conv_id)
        if key in seen:
            continue
        seen.add(key)
        if cached_conversation(network, platform, conv_id) is not None:
            continue  # already fresh (positive or negative) — skip the call
        work.append(key)

    if not work:
        return False

    any_resolved = False
    deadline = time.monotonic() + max_seconds
    ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="conv-enrich")
    try:
        futures = {
            ex.submit(
                resolve_conversation, network, platform, conv_id,
                bot_id=bot_id, use_cache=False,
            ): (platform, conv_id)
            for (platform, conv_id) in work
        }
        try:
            for fut in as_completed(
                futures, timeout=max(0.1, deadline - time.monotonic()),
            ):
                platform, conv_id = futures[fut]
                try:
                    fut.result(timeout=0)
                except Exception:  # noqa: BLE001
                    continue
                # Count a change iff this run wrote a FRESH entry (positive OR
                # negative sentinel — both are worth persisting). Checking with
                # the default TTL (not max_age_days=0) is deliberate: the work
                # list was pre-filtered to absent/stale ids, so the only way a
                # *fresh* entry exists now is that this run wrote it. The
                # no-token path writes nothing and leaves any pre-existing stale
                # entry stale, so it is correctly not counted (a max_age_days=0
                # check would mis-count that leftover stale entry as a change).
                if cached_conversation(network, platform, conv_id) is not None:
                    any_resolved = True
        except FuturesTimeout:
            log.debug(
                "conversation enrichment hit budget %ss with stragglers",
                max_seconds,
            )
    finally:
        ex.shutdown(wait=False)
    return any_resolved


def warm_conversation_cache(
    network: dict[str, Any], *,
    bot_id: "str | None" = None, days: int = 7, max_seconds: float = 20.0,
    network_path: "str | None" = None,
) -> bool:
    """Sweep entrypoint: warm the conversation cache from recent turn rollups.

    Pulls the distinct conversation ids seen in the last ``days`` of turns
    (reusing ``usage_analytics.load_turns`` — the same reader the Usage page
    and ``identity_discovery`` use), groups them by the dominant bot that
    served each conversation, and enriches each group with that bot's token
    (workspace-scoped — critical for Slack). Returns True iff any id resolved,
    so the caller can ``save_network``.

    Independent of any page load — schedulable as a daily cron, like
    ``signals.retention``. Render paths must NOT call this.
    """
    from collections import Counter, defaultdict

    try:
        from usage_analytics import load_turns
    except Exception:  # noqa: BLE001
        log.warning("usage_analytics.load_turns unavailable; cache not warmed")
        return False

    turns = load_turns(None, days=days, network_path=network_path)

    # conv_id → Counter(instance) so we can resolve each conversation with the
    # bot that actually served it most (its token is the one scoped to that
    # conversation's workspace). Mirrors the by_user ``instance`` heuristic.
    conv_instances: dict[str, Counter] = defaultdict(Counter)
    for t in turns:
        conv_id = normalize_conv_id(str(t.get("channel") or ""))
        if not conv_id:
            continue
        conv_instances[conv_id][t.get("instance") or ""] += 1

    if not conv_instances:
        return False

    groups: dict[str, list[str]] = defaultdict(list)
    for conv_id, counts in conv_instances.items():
        dominant = max(counts, key=lambda k: counts[k]) if counts else ""
        groups[dominant or (bot_id or "")].append(conv_id)

    # Split the wall-clock budget across the bot groups so the whole sweep
    # stays inside ``max_seconds``. No per-group floor (a floor × many groups
    # would blow the global budget); enrich already floors its internal
    # as_completed timeout at 0.1s. A hard global deadline backstops the sum:
    # once it passes we stop starting new groups.
    import time

    per_group = max_seconds / max(1, len(groups))
    deadline = time.monotonic() + max_seconds
    any_resolved = False
    for grp_bot, ids in groups.items():
        if time.monotonic() >= deadline:
            log.debug("conversation sweep hit %ss budget; %d group(s) deferred",
                      max_seconds, len(groups))
            break
        if enrich_conversation_names(
            network, ids,
            bot_id=grp_bot or bot_id, max_seconds=per_group,
        ):
            any_resolved = True
    return any_resolved


# ─── CLI sweep entrypoint ───────────────────────────────────────────────────


def main(argv: "list[str] | None" = None) -> int:
    """``python3 -m evolve_admin.evo.conversation_resolver`` — warm the cache.

    Loads ``network.json``, warms the conversation cache from recent turns,
    and persists if anything changed. Idempotent; safe to schedule daily.
    """
    import argparse

    from ..config import DEFAULT_NETWORK_CONFIG, load_network, save_network

    parser = argparse.ArgumentParser(
        description="Warm the conversation-name cache from recent turns.",
    )
    parser.add_argument(
        "--network-path", default=str(DEFAULT_NETWORK_CONFIG),
        help="Path to network.json (default: the canonical pod config).",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="How many days of turn rollups to scan (default: 7).",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=20.0,
        help="Wall-clock budget for the whole sweep (default: 20).",
    )
    args = parser.parse_args(argv)

    from pathlib import Path
    net_path = Path(args.network_path)
    network = load_network(net_path)
    changed = warm_conversation_cache(
        network, days=args.days, max_seconds=args.max_seconds,
        network_path=str(net_path),
    )
    if changed:
        save_network(network, net_path)
        log.info("conversation cache warmed; network.json updated")
    else:
        log.info("conversation cache: nothing new to resolve")
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
