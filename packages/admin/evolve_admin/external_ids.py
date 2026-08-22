"""evolve_admin.external_ids — the one reader/writer for a person's platform ids.

Invariant 6 (docs/spec-users-meta-2026-06-15.md §5): *one person is ONE roster
row; platform is a multi-valued attribute of that row.* The on-disk expression
of that is ``external_ids = {channel: [external_id, ...]}`` — a LIST per
channel, because a person can hold several ids on one platform (two Slack
workspaces, a personal + work Telegram) and will hold ids on several platforms
at once as soon as M1 lands.

Two live shapes, one reader
---------------------------
``network.json`` on a real pod carries BOTH shapes today:

* ``pod.admins.external_ids``            → ``{"telegram": ["<id>"]}``  (list)
* ``bots.<id>.primary_user.external_ids`` → ``{"slack": "<id>"}``      (scalar)

The documented schema (``config.py`` pod-defaults) has always said list; the
per-bot writers have always written scalar. Consumers picked one and were wrong
against the other. The sharpest failure was ``breakers_enforce`` doing
``str(external_ids[channel])`` — against the list shape that yields the literal
string ``"['123']"``, which then gets handed downstream as a chat id and the
send silently goes nowhere. Its mirror in ``roster_overlay`` had the inverse
bug: ``{str(x) for x in ext.get(platform)}`` iterates a bare string
CHARACTER BY CHARACTER, so a scalar entry expands into a set of digits.

:func:`read_external_ids` accepts both and always returns the list shape. It is
the ONLY sanctioned way to read this field — never index the raw dict.

**No migration.** There is deliberately no rewrite pass over ``network.json``.
The tolerant reader IS the compatibility layer, so a migration would buy
nothing it does not already give us while coupling this change to a deploy
step. Writers normalize opportunistically: any entry that gets written through
:func:`set_channel_ids` / :func:`add_external_id` lands in list shape, and the
file converges on its own.

Why a top-level module and not part of ``evo.identity``
--------------------------------------------------------
``evo.identity`` owns the *policy* over these ids (who is admin, who may claim
a bot, what a re-claim collision means) and lives inside the evo gateway's
import graph. The *shape* is consumed far outside that graph — ``alerts``,
``breakers_enforce``, ``roster_overlay``, ``briefing_activation``, ``cli``,
``applications`` — and none of those should have to import the evo package to
read a chat id. So this sits next to :mod:`evolve_admin.channel_registry`, its
opposite number: that module describes channels and never users, this one
describes users' channel ids and never channels. Both are import-light on
purpose (stdlib + each other only).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from . import channel_registry

__all__ = [
    "EXTERNAL_IDS_KEY",
    "normalize",
    "read_external_ids",
    "ids_for",
    "first_id_for",
    "has_id",
    "channels_with_ids",
    "all_ids",
    "write_external_ids",
    "set_channel_ids",
    "add_external_id",
    "remove_external_id",
    "resolve_notify_target",
]

EXTERNAL_IDS_KEY = "external_ids"


# ── Normalization ───────────────────────────────────────────────────────


def _clean_channel(channel: Any) -> Optional[str]:
    """Canonical channel key, or None if unusable.

    Lower-cased and stripped, matching what ``evo.identity.claim_primary``
    has always done on the write side — so a hand-edited ``"Telegram"`` key
    resolves the same as a claimed ``"telegram"`` one.
    """
    if not isinstance(channel, str):
        return None
    cleaned = channel.strip().lower()
    return cleaned or None


def _clean_ids(value: Any) -> list[str]:
    """Coerce one channel's value — either shape — to a clean id list.

    * ``"U123"``          → ``["U123"]``   (the scalar shape)
    * ``["U123", "U456"]`` → unchanged      (the documented shape)
    * ``123``             → ``["123"]``    (hand-edited JSON number)
    * ``""`` / ``[]`` / ``None`` / ``{}`` → ``[]``

    Order is preserved and duplicates are dropped, so the first id stays the
    first id (the notify path depends on that — see
    :func:`resolve_notify_target`).
    """
    if value is None or isinstance(value, (dict, Mapping)):
        return []
    if isinstance(value, (str, int)):
        items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or isinstance(item, (dict, list, tuple, set, frozenset)):
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize(raw: Any) -> dict[str, list[str]]:
    """Normalize a raw ``external_ids`` MAPPING to ``{channel: [id, ...]}``.

    Tolerant of every shape seen in the wild: a missing/None/non-dict value
    yields ``{}``; channels whose ids all wash out are dropped entirely, so
    ``if ids.get(ch)`` and ``if ch in ids`` agree.

    Use :func:`read_external_ids` when you have the containing block
    (``primary_user`` / ``pod.admins``); this is for the rarer case where a
    caller already holds the inner mapping.
    """
    if not isinstance(raw, (dict, Mapping)):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        channel = _clean_channel(key)
        if channel is None:
            continue
        ids = _clean_ids(value)
        if not ids:
            continue
        # A hand-edited file could carry both "Telegram" and "telegram";
        # they are the same channel, so merge rather than clobber.
        if channel in out:
            for ext_id in ids:
                if ext_id not in out[channel]:
                    out[channel].append(ext_id)
        else:
            out[channel] = ids
    return out


def read_external_ids(block: Any) -> dict[str, list[str]]:
    """``{channel: [external_id, ...]}`` for a ``primary_user`` / ``admins`` block.

    THE reader. Accepts both live shapes (see the module docstring) and always
    returns the list shape, with empties dropped. Never raises — a missing
    block, a ``None``, or a corrupt non-dict all yield ``{}``.

    The returned dict is a fresh copy; mutating it does NOT write back to
    ``network``. Use :func:`set_channel_ids` / :func:`add_external_id` for that.
    """
    if not isinstance(block, (dict, Mapping)):
        return {}
    return normalize(block.get(EXTERNAL_IDS_KEY))


def ids_for(block: Any, channel: Any) -> list[str]:
    """Every id this person holds on ``channel`` (possibly empty)."""
    cleaned = _clean_channel(channel)
    if cleaned is None:
        return []
    return read_external_ids(block).get(cleaned, [])


def first_id_for(block: Any, channel: Any) -> Optional[str]:
    """This person's primary id on ``channel``, or None.

    "First" is positional, not semantic — see :func:`resolve_notify_target`
    for why the first id is the one a send path uses.
    """
    ids = ids_for(block, channel)
    return ids[0] if ids else None


def has_id(block: Any, channel: Any, external_id: Any) -> bool:
    """True iff ``external_id`` is recorded for this person on ``channel``.

    String-compares after stripping, so an int in JSON and a string from a
    gateway header match.
    """
    if external_id is None:
        return False
    needle = str(external_id).strip()
    if not needle:
        return False
    return needle in ids_for(block, channel)


def channels_with_ids(block: Any) -> tuple[str, ...]:
    """Channels this person is reachable on, in recorded order."""
    return tuple(read_external_ids(block))


def all_ids(block: Any) -> tuple[tuple[str, str], ...]:
    """Flat ``((channel, external_id), ...)`` over every recorded id."""
    return tuple(
        (channel, ext_id)
        for channel, ids in read_external_ids(block).items()
        for ext_id in ids
    )


# ── Writers (always emit the list shape) ────────────────────────────────


def write_external_ids(block: dict, mapping: Any) -> dict[str, list[str]]:
    """Replace the whole ``external_ids`` map on ``block``, normalized.

    THE writer. ``block`` is mutated in place and the stored (normalized)
    mapping is returned. Caller persists via
    :func:`evolve_admin.config.save_network`.
    """
    if not isinstance(block, dict):
        raise TypeError("write_external_ids needs a mutable dict block")
    normalized = normalize(mapping)
    block[EXTERNAL_IDS_KEY] = normalized
    return normalized


def set_channel_ids(block: dict, channel: Any, ids: Any) -> list[str]:
    """Set (replace) the id list for ONE channel, leaving the others alone.

    Passing an empty/None ``ids`` removes the channel key entirely, keeping
    the "no empty lists on disk" invariant the reader relies on.
    """
    if not isinstance(block, dict):
        raise TypeError("set_channel_ids needs a mutable dict block")
    cleaned = _clean_channel(channel)
    if cleaned is None:
        raise ValueError("channel is required (non-empty string)")
    current = read_external_ids(block)
    new_ids = _clean_ids(ids)
    if new_ids:
        current[cleaned] = new_ids
    else:
        current.pop(cleaned, None)
    block[EXTERNAL_IDS_KEY] = current
    return list(new_ids)


def add_external_id(block: dict, channel: Any, external_id: Any) -> list[str]:
    """Append one id to a channel's list (idempotent). Returns the new list.

    Whatever shape the entry was in before, it comes back out as a list —
    this is the opportunistic normalization that lets us skip a migration.
    """
    if not isinstance(block, dict):
        raise TypeError("add_external_id needs a mutable dict block")
    cleaned = _clean_channel(channel)
    if cleaned is None:
        raise ValueError("channel is required (non-empty string)")
    text = str(external_id).strip() if external_id is not None else ""
    if not text:
        raise ValueError("external_id is required (non-empty)")
    current = read_external_ids(block)
    ids = current.get(cleaned, [])
    if text not in ids:
        ids = [*ids, text]
    current[cleaned] = ids
    block[EXTERNAL_IDS_KEY] = current
    return list(ids)


def remove_external_id(block: dict, channel: Any, external_id: Any) -> bool:
    """Drop one id from a channel. True iff something was removed.

    An emptied channel key is deleted rather than left as ``[]``.
    """
    if not isinstance(block, dict):
        return False
    cleaned = _clean_channel(channel)
    if cleaned is None:
        return False
    text = str(external_id).strip() if external_id is not None else ""
    if not text:
        return False
    current = read_external_ids(block)
    ids = current.get(cleaned)
    if not ids or text not in ids:
        # Still rewrite in normalized form only if the key exists — never
        # touch the file shape on a pure miss.
        return False
    ids = [i for i in ids if i != text]
    if ids:
        current[cleaned] = ids
    else:
        current.pop(cleaned, None)
    block[EXTERNAL_IDS_KEY] = current
    return True


# ── Notify-recipient resolution (D2) ────────────────────────────────────


def resolve_notify_target(
    block: Any,
    *,
    primary_channel: Any = None,
    priority: Optional[Iterable[str]] = None,
) -> Optional[tuple[str, str]]:
    """Return ``(channel, chat_id)`` to notify this person on, or None.

    Operator decision D2 (spec §M1): ``primary_channel`` stays **singular —
    the notify preference only**. Pairing and admission go plural; "which
    channel Evolve talks to you on" is deliberately a different cardinality
    from "which channels you can reach the bot on". So:

      1. ``primary_channel``, if that channel has an id recorded.
      2. Otherwise the registry's notify-fallback order
         (:func:`channel_registry.by_notify_priority`) — never a local tuple
         (invariant 7).
      3. Otherwise None; the caller decides whether to log + skip.

    **Multiple ids on the chosen channel → we send to the FIRST, not all.**
    Deliberate, not an accident of ``[0]``. Every caller of this function is
    an ALERT path (circuit-breaker OOO notices, operator alerts), and alerts
    are the one place where fan-out is actively harmful: N ids would multiply
    the same warning N times, and the duplicate copies land on the same human
    who already read the first one. A person's extra ids on one channel are
    additional *admission* keys (D1 — one row, many keys), not additional
    mailboxes that each want a copy. If a genuine broadcast surface appears
    later it should iterate :func:`ids_for` explicitly and own that decision
    itself, rather than this resolver silently changing meaning underneath
    the alert paths.
    """
    ids = read_external_ids(block)
    if not ids:
        return None

    explicit = _clean_channel(primary_channel)
    if explicit and ids.get(explicit):
        return explicit, ids[explicit][0]

    order = tuple(priority) if priority is not None else channel_registry.by_notify_priority()
    for channel in order:
        cleaned = _clean_channel(channel)
        if cleaned and ids.get(cleaned):
            return cleaned, ids[cleaned][0]
    return None
