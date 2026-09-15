"""Discover a bot's primary/owner user from its turn history.

When the operator runs the setup wizard on a pod that's already been
in use for a few days — or hits the "Discover from history" button
on the Identity subtab — we'd like to pre-fill the per-bot
``primary_user`` block with the most-likely owner identity instead of
making them type the IDs in by hand.

Two producers write the turn-record JSONL files we scan:

  1. **OC turn-collector** (``/Users/Shared/openclaw-usage/turn-collector.py``)
     emits the canonical schema into
     ``/Users/<bot>/.openclaw/workspace/memory/turns-<date>.jsonl``::

         source:  "human" | "cron" | "subagent"
         channel: "telegram" | "slack" | "discord" | …
         user_id: "<external_id>"   # chat_id, U… id, etc.

  2. **Plugin TurnObserver** (``packages/plugin/.../TurnObserver.ts``)
     emits a slightly different schema into
     ``/Users/Shared/evolve/<bot>/turns/turns-<date>.jsonl``::

         source:  "user" | "heartbeat" | "cron" | "subagent" | "unknown"
         channel: "<gateway-supplied channel id>"   # for Telegram
                                                    # this is the chat_id;
                                                    # for Slack a D…/U…/C…
                                                    # id; for Discord the
                                                    # 17-19 digit snowflake.
         user_id: null   # hard-coded; the gateway doesn't expose it

Both shapes show up on real pods (the converter at
``packages/analyzer/cost_event_converter.py`` likewise normalizes
``source=='user'`` → ``'human'`` and consumes either path). We tally
``(platform, external_id)`` across the last N days of turns, drop
non-human rows, and return the top K candidates sorted by frequency.
The caller (route or wizard) then runs each candidate through
``name_resolver.resolve`` to fill in the display name.

This module assumes the ``evolve`` user has ACL read on
``/Users/<bot>/.openclaw/`` — set during deploy by
``set_evolve_read_acl``. No sudo needed; we fall through paths that
exist and skip the rest.

Reliability:

  * **Single-user bots** — high. Every turn shares one sender; the
    discovery is unambiguous.
  * **Multi-user bots** — good when usage is dominated by the owner.
    Could be wrong when usage is balanced; the caller should show
    the top 2-3 candidates with counts and let the operator pick.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Recognized platform names that may appear as a literal in the
# ``channel`` field (OC turn-collector format).
_PLATFORM_NAMES: frozenset[str] = frozenset({
    "telegram", "slack", "discord", "whatsapp",
})

# Values of ``source`` we count as a human turn. The OC turn-collector
# writes ``"human"``; the plugin TurnObserver writes ``"user"`` for the
# same thing (see ``cost_event_converter._normalize_evolve_turn``).
# Missing/empty ``source`` falls through to channel-based classification
# rather than being discarded outright — older deploys didn't always
# populate the field.
_HUMAN_SOURCES: frozenset[str] = frozenset({"human", "user", ""})

# Channel values that explicitly mean "non-human" — drop early without
# pretending to infer a platform from them.
_NON_HUMAN_CHANNELS: frozenset[str] = frozenset({
    "unknown", "heartbeat", "cron", "cron-event", "exec-event",
    "subagent", "webchat",
})

# ``user_id`` sentinel values the OC turn-collector emits when it can't
# infer the real platform id. Treat as missing — otherwise discovery
# surfaces a high-traffic candidate with ``external_id="unknown"`` that
# the operator can't act on (see the admin_bot chip 2026-05-29).
_NON_HUMAN_USER_IDS: frozenset[str] = frozenset({"unknown", "none", "null"})

# Discord snowflakes are always at least this many digits; below this
# we assume a numeric channel id belongs to Telegram. Telegram personal
# chat_ids top out around 10 digits today; group chat_ids go higher but
# also negative (the leading '-' is not counted by ``isdigit``).
_DISCORD_SNOWFLAKE_MIN_DIGITS = 15


def _turn_dir_candidates(bot_id: str, bot_user: str | None) -> list[Path]:
    """All known places a bot's ``turns-*.jsonl`` files might live.

    The new-style shared location is preferred (world-readable); the
    workspace memory dir is the standard OC plugin location; the
    home-derived fallback covers older deploys.
    """
    if bot_user:
        home = Path(f"/Users/{bot_user}")
    else:
        # Canonical resolution — bot_id may differ from the macOS account
        # (the old local helper fell back to /Users/{bot_id}, which silently
        # missed turns for renamed-account bots like team_bot_b).
        from evolve_config import bot_home
        home = bot_home(bot_id)
    candidates: list[Path] = [
        Path(f"/Users/Shared/evolve/{bot_id}/turns"),
        home / ".openclaw" / "workspace" / "memory",
    ]
    return candidates


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file. Returns [] when the file's missing, the dir
    isn't readable, or the file's corrupt. Don't call ``exists()``
    first — that can itself raise PermissionError if the parent dir
    isn't readable; we let the open() handler swallow it."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            out: list[dict] = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
            return out
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return []
    except OSError:
        return []


def _iter_turns(
    bot_id: str, bot_user: str | None, lookback_days: int,
) -> list[dict]:
    """Yield deduplicated turn records for ``bot_id`` over the last
    ``lookback_days`` days, scanning every candidate dir."""
    # Producers (TurnObserver, OC turn-collector) name files by UTC date.
    # Using local `date.today()` here would skip today's file during the
    # local-evening window after UTC midnight rolls over.
    today = datetime.now(tz=timezone.utc).date()
    cutoff = today - timedelta(days=max(1, lookback_days))
    out: list[dict] = []
    seen: set[str] = set()
    dirs = _turn_dir_candidates(bot_id, bot_user)
    cur = cutoff
    while cur <= today:
        d_str = cur.isoformat()
        for d in dirs:
            fname = d / f"turns-{d_str}.jsonl"
            for rec in _read_jsonl(fname):
                # Dedup across candidate dirs by (ts, session_id) so
                # bots that mirror their turns into both the shared
                # dir AND the workspace dir don't double-count.
                key = f"{rec.get('ts', '')}:{rec.get('session_id', '')}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
        cur = cur + timedelta(days=1)
    return out


def _infer_platform(ch: str) -> "str | None":
    """Infer the messaging platform from the shape of a channel string.

    ``ch`` is either a literal platform name (OC turn-collector format)
    or a gateway-supplied identifier (plugin TurnObserver format: a
    Telegram chat_id, a Slack ``C/D/G/U/W`` id, or a Discord snowflake).
    Returns the normalized platform name, or ``None`` when the shape
    matches nothing we can confidently attribute.

    Shape rules only — no provider/model literals — so this stays
    provider-neutral as new platforms arrive.
    """
    if ch in _PLATFORM_NAMES:
        return ch
    # Numeric (Telegram chat_id or Discord snowflake). Strip a single
    # leading '-' (Telegram group chats are negative) before counting.
    digits = ch[1:] if ch.startswith("-") else ch
    if digits and digits.isdigit():
        if len(digits) >= _DISCORD_SNOWFLAKE_MIN_DIGITS:
            return "discord"
        return "telegram"
    # Slack DM / user / channel / conversation id. Keys are 8+ chars and
    # start with one of D / U / W / G / C, then alnum. Slack also
    # produces lowercase ``c<id>`` and ``c<id>:thread:<ts>`` forms;
    # match on the part before any ``:thread:`` suffix.
    head = ch.split(":", 1)[0]
    if len(head) >= 8 and head[0].lower() in {"d", "u", "w", "g", "c"} \
            and head[1:].isalnum():
        return "slack"
    return None


def _extract_identity(
    channel: str, user_id: str,
) -> tuple[str, str] | None:
    """Map a turn record's ``(channel, user_id)`` pair to a normalized
    ``(platform, external_id)`` tuple, or ``None`` when the row carries
    no usable identifying info.

    Handles both schemas described in the module docstring, plus the
    real-world hybrid that the old code mishandled:

      * OC turn-collector: ``channel`` is a platform name and
        ``user_id`` is the per-platform external id.
      * Plugin TurnObserver (no sender enrichment): ``user_id`` is
        ``null`` and ``channel`` is the gateway-supplied conversation
        identifier (Telegram chat_id, Slack ``C/D/G/U/W`` id, Discord
        snowflake). We infer the platform from the channel string shape
        and use the channel itself as the external id.
      * Plugin TurnObserver WITH a real ``user_id`` — the common case on
        live Slack: a group turn carries ``channel="c07…:thread:…"``
        (a CONVERSATION id) *plus* ``user_id="U07…"`` (the actual
        sender). The earlier code branched on the conversation-shaped
        channel and dropped the ``user_id``, surfacing the conversation
        as the "identity" — so high-activity group/DM senders never
        reached the Users page even though Usage's By-User saw them. We
        now PREFER a present, valid ``user_id`` and attribute to the
        person, inferring the platform from the channel shape.

    Returns ``None`` for: missing/empty channel, channel values that
    explicitly mean non-human (``unknown``, ``heartbeat``, …), or
    shapes we can't confidently attribute to a platform.
    """
    ch = (channel or "").strip()
    uid = (user_id or "").strip()
    if uid.lower() in _NON_HUMAN_USER_IDS:
        # Sentinel id ("unknown"/"none"/"null") is not a real person —
        # treat as absent so we fall back to channel-based extraction
        # rather than surfacing ("slack", "unknown").
        uid = ""
    if not ch or ch in _NON_HUMAN_CHANNELS:
        return None

    platform = _infer_platform(ch)

    # Prefer a real sender id over the channel. The gateway stamps the
    # actual ``user_id`` on group/conversation turns; that person is who
    # the operator wants to admit, not the conversation. Only attribute
    # when we can name the platform from the channel shape — otherwise
    # fall through so we don't pair a real id to a guessed platform.
    if uid and platform is not None:
        return (platform, uid)

    # No usable user_id (older plugin emissions leave it null) — fall
    # back to channel-as-identity extraction.
    if ch in _PLATFORM_NAMES:
        # Literal platform name with no external id → nothing to
        # attribute. Skip rather than emitting an empty external_id,
        # which the UI would render as "telegram (no id)" — confusing.
        return None
    if platform == "slack":
        # The channel IS the external id; collapse any ``:thread:``
        # suffix to the base conversation id.
        return ("slack", ch.split(":", 1)[0])
    if platform is not None:
        return (platform, ch)

    # Anything else (e.g. ``cron-event:...``, free-form labels, future
    # platforms) — drop rather than guess. We'd rather miss a candidate
    # than mis-attribute one to the wrong platform.
    return None


def discover_candidates(
    bot_id: str,
    bot_user: str | None = None,
    *,
    lookback_days: int = 30,
    top_k: int = 5,
) -> list[dict]:
    """Return up to ``top_k`` candidate primary-user identities for
    ``bot_id``, sorted by turn-count descending.

    Each candidate dict has shape::

        {
          "channel":      "telegram" | "slack" | ...,
          "external_id":  "123456789",
          "turn_count":   42,
          "last_seen":    "2026-05-19T14:32:00Z",  # ISO8601 or ""
          "first_seen":   "2026-05-15T08:00:00Z",
        }

    Filters:
      * ``source`` not in ``{human, user, ""}`` is dropped (cron,
        subagent, heartbeat, …).
      * Rows from which we can't extract a ``(platform, external_id)``
        pair are dropped — see ``_extract_identity``.

    Returns an empty list when no turn history exists (newly-deployed
    bot, or no human turns). The caller's UI handles that case with
    a "no history yet, enter manually" message.
    """
    tallies: dict[tuple[str, str], dict[str, Any]] = {}

    for rec in _iter_turns(bot_id, bot_user, lookback_days):
        source = (rec.get("source") or "").strip().lower()
        if source not in _HUMAN_SOURCES:
            continue
        identity = _extract_identity(
            rec.get("channel") or "", str(rec.get("user_id") or ""),
        )
        if identity is None:
            continue
        channel, external_id = identity
        key = (channel, external_id)
        ts = rec.get("ts") or ""
        entry = tallies.get(key)
        if entry is None:
            tallies[key] = {
                "channel": channel,
                "external_id": external_id,
                "turn_count": 1,
                "first_seen": ts,
                "last_seen": ts,
            }
        else:
            entry["turn_count"] += 1
            if ts and (not entry["first_seen"] or ts < entry["first_seen"]):
                entry["first_seen"] = ts
            if ts and (not entry["last_seen"] or ts > entry["last_seen"]):
                entry["last_seen"] = ts

    # Sort by turn_count desc, then most-recent last_seen as
    # tiebreaker — a recently-active user with the same count as a
    # long-quiet user should rank higher.
    ordered = sorted(
        tallies.values(),
        key=lambda c: (c["turn_count"], c["last_seen"]),
        reverse=True,
    )
    return ordered[:top_k]


def _rewrite_slack_dm_candidates(
    network: dict[str, Any],
    candidates: list[dict],
    *,
    bot_id: str | None = None,
) -> list[dict]:
    """For Slack candidates whose ``external_id`` is an IM channel id
    (``D...``), resolve the channel to its participating user (``U...``)
    and rewrite the candidate so downstream consumers (name resolver,
    "Use" affordance, primary-user form) operate on the actual user id.

    Background: turn records from the plugin TurnObserver write the
    sessionKey trailing id into the ``channel`` field. For Slack DMs
    that's the IM channel id, not the user — so `discover_candidates`
    emits D-prefixed candidates that the operator can't directly act
    on. F.1 rewires this in the discovery path; the D-id is preserved
    on the candidate as ``via_channel`` for traceability.

    Falls back to the candidate as-is when:
      - the candidate isn't Slack
      - the external_id doesn't start with D
      - no Slack token is available for the bot
      - conversations.info fails or returns a non-IM channel
    """
    from . import name_resolver

    # Memoize per-D-id so a candidate list with two records keyed on
    # the same D-id (shouldn't happen for IMs but defensive) doesn't
    # hit conversations.info twice.
    cache: dict[str, str | None] = {}

    out: list[dict] = []
    for c in candidates:
        if c.get("channel") != "slack":
            out.append(c)
            continue
        eid = (c.get("external_id") or "").strip()
        if not eid or not eid.startswith(("D", "d")):
            out.append(c)
            continue
        if eid not in cache:
            token = name_resolver._channel_token(
                network, "slack", prefer_bot_id=bot_id)
            cache[eid] = (
                name_resolver.slack_im_channel_to_user(token, eid)
                if token else None
            )
        user_id = cache[eid]
        if not user_id:
            out.append(c)
            continue
        rewritten = dict(c)
        rewritten["external_id"] = user_id
        rewritten["via_channel"] = eid
        out.append(rewritten)
    return _dedupe_by_channel_and_id(out)


def _dedupe_by_channel_and_id(candidates: list[dict]) -> list[dict]:
    """Merge candidates that share a ``(channel, external_id)`` key.

    After the D→U rewrite, the same user can show up via two paths:
      - A D-channel record (TurnObserver wrote the IM channel id into
        the ``channel`` field; D→U rewrite then mapped it to the user).
      - A direct user_id record (D.1's senderRegistry enriched the
        turn record's ``user_id`` field directly).

    Both represent the same identity. Without deduping the operator
    sees a "Greg Alden, 19 turns" row followed by "Greg Alden, 6 turns
    (via D0AMYBZ4RM1)" — confusing and double-counts activity.

    Merge strategy:
      - sum ``turn_count`` (the real total across both paths)
      - earliest ``first_seen``, latest ``last_seen``
      - preserve ``via_channel`` from any path that has one
    Stable ordering: emit in the order of the first occurrence of
    each key so the upstream sort (count desc) survives roughly intact.
    """
    by_key: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for c in candidates:
        key = (c.get("channel") or "", c.get("external_id") or "")
        if not key[0] or not key[1]:
            # Don't try to dedupe broken entries; emit as-is.
            sentinel = (None, id(c))  # unique key, won't collide
            by_key[sentinel] = c  # type: ignore[index]
            order.append(sentinel)  # type: ignore[arg-type]
            continue
        if key not in by_key:
            by_key[key] = dict(c)
            order.append(key)
            continue
        merged = by_key[key]
        merged["turn_count"] = (
            int(merged.get("turn_count") or 0)
            + int(c.get("turn_count") or 0)
        )
        # ISO timestamps sort lexically; min/max gives the right bound.
        f1 = merged.get("first_seen") or ""
        f2 = c.get("first_seen") or ""
        merged["first_seen"] = min(f1, f2) if (f1 and f2) else (f1 or f2)
        l1 = merged.get("last_seen") or ""
        l2 = c.get("last_seen") or ""
        merged["last_seen"] = max(l1, l2)
        if not merged.get("via_channel") and c.get("via_channel"):
            merged["via_channel"] = c["via_channel"]
    return [by_key[k] for k in order]


def resolve_with_names(
    network: dict[str, Any],
    candidates: list[dict],
    *,
    bot_id: str | None = None,
) -> list[dict]:
    """Run each candidate through ``name_resolver.resolve`` to attach
    a username / display name when the platform supports it.

    Returns the same list of candidates with two new fields per entry::

        "username": "<platform handle>" | None,
        "display_name": "<best-effort human label>" | None,

    Slack D-channel candidates are pre-rewritten to their U-user form
    via ``_rewrite_slack_dm_candidates`` before name lookup, so the
    candidate's ``external_id`` is the actual user id the operator
    can act on (paste into the primary form, pair, etc.).

    ``bot_id`` is the bot whose tokens we prefer for the per-channel
    lookup — critical for Slack where tokens are workspace-scoped.
    See ``name_resolver._channel_token`` for the routing rules.

    Failures are silent — the entry's fields just stay None. The
    name resolver writes its cache into ``network`` as a side effect
    (caller persists via ``save_network``).
    """
    # Lazy import — avoids a circular import if anyone uses this
    # module from inside name_resolver in the future.
    from . import name_resolver

    candidates = _rewrite_slack_dm_candidates(
        network, candidates, bot_id=bot_id)

    out: list[dict] = []
    for c in candidates:
        resolved = name_resolver.resolve(
            network,
            channel=c["channel"],
            external_id=c["external_id"],
            bot_id=bot_id,
        )
        entry = dict(c)
        if resolved:
            entry["username"] = resolved.get("username")
            entry["display_name"] = resolved.get("name")
        else:
            entry["username"] = None
            entry["display_name"] = None
        out.append(entry)
    return out
