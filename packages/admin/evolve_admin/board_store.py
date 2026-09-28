"""board_store.py — the Board's single-writer store (slice 1).

Design: ``internal/design-pa-mobile-board-2026-08-31.md`` (D-MB1: one
writer). The store lives under the pod's shared dir and is written ONLY by
the admin daemon (this module); the bot and the mobile page both go through
daemon endpoints. Layout:

    {shared_dir}/boards/<bot_id>/
    ├── board.json                # canonical card store
    ├── events/<YYYY-MM-DD>.jsonl # append-only interaction log (learning loop §1)
    └── token.sha256              # sha256 of the per-user board token, 0600

The token is minted operator-side and shown ONCE; only its hash is stored,
so a read of the shared dir never yields a usable credential. Verification
is constant-time. There is no "no token" open mode — an unminted board
refuses every request (fail closed).

OWNERSHIP. Every writer below is reachable BOTH in-process as the admin
daemon (``evolve``) and from ``sudo evolve-admin board …`` as root, so each
one adopts what it writes to the daemon user (``_adopt_store`` ->
``board_store_perms.adopt``; a no-op unless we are root). Without that, a
root-minted 0600 token hash is one the daemon cannot open, and the board
answers a plain 401 to a link that is perfectly valid — the 2026-09-04 phone
test. ``board_store_perms`` carries the full incident note and the
``ensure_pod_perms`` drift check that re-verifies it.

Operator entry points (module-runnable to keep the size-capped ``cli.py``
untouched in this slice):

    python3 -m evolve_admin.board_store mint --bot <id> [--network <path>]
    python3 -m evolve_admin.board_store revoke --bot <id> [--network <path>]
    python3 -m evolve_admin.board_store import-tasks --bot <id> --tasks-file <p> [--network <path>]

``sudo evolve-admin board token|revoke <bot_id>`` (board_cli.py) is the
operator-facing wrapper D-MB2 specified; this module stays the
implementation, so there is one mint path, not two.

``import-tasks`` is the D-MB6 seed: it parses a Task-Manager-style markdown
task list (tables with ``| # | Task | Context | Who |`` rows) into inbox
cards, skipping completed sections. It never deletes existing cards and is
idempotent per title (a card whose title already exists is not re-added).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import board_actions
from .board_store_perms import REPAIR_COMMAND, adopt

log = logging.getLogger(__name__)

#: Lanes answer WHEN (D-BI7, ratified 2026-09-04): ``inbox · today · later ·
#: done · dropped``. ``bot`` is deliberately absent — "who" is the separate
#: :data:`OWNERS` attribute, and the former Bot lane is a VIEW derived from
#: ``owner == "bot"``. :func:`migrate_board` converts any card still sitting
#: in the retired lane on load.
LANES = ("inbox", "today", "later", "done", "dropped")

#: The lane D-BI7 retired. Only ever read (by the migration), never written.
LEGACY_BOT_LANE = "bot"

#: Who a card is on (D-BI7), widened by D-TM7. DEVIATION, disclosed: the
#: design's prose calls the shipped D-BI7 value "user"; THIS store keeps
#: "me" — the value `routes_board.py`/`board_actions.py`/`board.html`/
#: `stack.html` (none touched here) and every existing test already check.
#: Renaming it is a separate chip; a schema-only slice must not break them.
#: `other` is the one new value (D-TM7) and REQUIRES `waiting_on` — see
#: :data:`WAITING_ON_KEYS`.
OWNERS = ("me", "bot", "other")

#: D-TM2 "other": who the card is waiting on, why, and since when — the
#: same discipline `enrichment{}` already keeps (every field names its
#: source), applied to the one new owner state. `who`/`source` are
#: required; `since` is stamped here when absent, same as `enrichment`'s
#: `captured_at`.
WAITING_ON_KEYS = ("who", "since", "source")

#: D-TM2: a maintenance-style report's severity, 1 (worst) to 3 (best) —
#: the vocabulary the Project Manager application's Slack reports already
#: use in the field (design-pa-tasks-and-follow-through-2026-09-18.md §1).
SEVERITIES = (1, 2, 3)

#: D-TM5: the five pace classes, first-touch-soonest to slowest. Order
#: matters only for display; :data:`CADENCE_DEFAULTS` is keyed the same way.
CADENCE_CLASSES = ("now", "today", "soon", "later", "someday")

#: D-TM3's four touch shapes — what the (future) scheduler does when
#: `next_touch` arrives. Fixed here because :func:`set_pace`/
#: :func:`record_touch` enforce membership; nothing in THIS chip fires one
#: (guardrail) — the vocabulary just needs to exist for a card to carry it.
TOUCH_ACTIONS = ("remind", "check_source", "do_action", "ask")

#: D-TM8: the field name a card's (at most one, one-level) goal link lives
#: under. A bare string constant, not a tuple, so :func:`set_goal` and a
#: reader can both name it without repeating the literal.
GOAL_BELONG_TO_KEY = "belong_to"

#: D-TM1 build item 0: a list's two shapes. `assistant` = one person + their
#: bot (a personal board); `project` = many members, human-quotable ids.
LIST_SHAPES = ("project", "assistant")

#: The list every bot's board already effectively had before this chip: its
#: own personal assistant list. :func:`ensure_personal_list` mints this
#: list (once, idempotently) on every board that doesn't yet have one, and
#: every card with no `list_id` reads as belonging to it — "existing cards
#: migrate to the bot's personal assistant list" (build item 0). Never a
#: project id_prefix.
DEFAULT_LIST_ID = "personal"

#: Lanes a card is FINISHED in — the two that settle it. They are what
#: :func:`is_archived` ages out of the default board read and what
#: :func:`find_settled_duplicate` dedups stocking against: both questions are
#: "has this already been decided?", and the answer is the same set.
SETTLED_LANES = ("done", "dropped")

#: The one optional tap-reason a drop may carry (D-BI2, verbatim). A fixed
#: set, not free text: this is the learning loop's negative signal, and a
#: detector can only count reasons it can compare. ``None`` stays valid — the
#: gesture must never require a second tap.
DROP_REASONS = ("not mine", "already handled", "never", "later than later")

#: How long a settled card's TILE stays on the board (D-BI2, answer to §6 Q2:
#: "keep the record forever, hide the tile after 30 days"). Nothing is ever
#: deleted — :func:`is_archived` is a read-side filter and the card stays in
#: ``board.json`` as learning data, reachable with ``?include=archived``.
ARCHIVE_AFTER_DAYS = 30

#: Delegation lifecycle (design-pa-board D-PA6 §"Delegation"). ``offered`` is
#: what an assignment to the bot creates; the bot drives the rest through
#: ``board.progress``.
DELEGATION_STATES = (
    "offered", "accepted", "in_progress", "returned_for_review", "done", "blocked",
)
#: D-PA5 cluster vocabulary — health and fitness deliberately separate;
#: custom clusters are allowed in card data (validated as slugs, not
#: against this tuple), this is the canonical starter set.
CLUSTERS = (
    "health", "fitness", "travel", "work", "social",
    "hobbies", "family", "home", "admin",
)

_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: A project list's human-id prefix (``OP``, ``GD``, ``MX``) — 2-6 upper-
#: case letters, matching the maintenance/agenda/project examples in the
#: design doc's §1.
_ID_PREFIX_RE = re.compile(r"^[A-Z]{2,6}$")
_LIST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MEMBER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

SCHEMA_VERSION = 1

#: Per-card and per-board bounds (review F-8). The write API caps the request
#: BODY at 16 KB, which bounds one write but not the store: 16 KB of title,
#: repeated, is a slow unbounded growth path for anyone holding the token.
#: These are the store-side bounds — generous for a human writing a task,
#: refusing anything that is plainly not one.
MAX_TITLE_CHARS = 500
MAX_NOTE_CHARS = 4000
MAX_CARDS = 5000

#: Enrichment bounds (addendum ``design-pa-lists-and-board-2026-09-01.md`` §2).
#: Enrichment is captured ONCE, at add time, from what the bot already knows —
#: it is a handful of facts about a card, not a document store. These bounds
#: say so in code, because the write API's 16 KB body cap bounds one request
#: and not the card it lands on.
MAX_ENRICHMENT_FIELDS = 20
MAX_ENRICHMENT_CHARS = 4000

#: The three keys an enrichment FIELD may carry. Every field names where its
#: value came from and when — a field without provenance is a claim nobody
#: can check later, which is the failure this shape exists to prevent.
ENRICHMENT_FIELD_KEYS = ("value", "source", "captured_at")

#: D-BI3: these four field names carry a typed ``value`` — validated below by
#: :data:`_TYPED_ENRICHMENT_VALIDATORS` — alongside the free-form fields chip
#: 1 already shipped (``runtime_min``, ``pages``, ``why_saved``, …), which
#: keep taking any JSON ``value`` unchanged.
MAX_CONTACTS = 8
MAX_LINKS = 8

#: D-TM2/5/8 bounds — same "generous for a human, refuses anything that is
#: plainly not one" bias as the bounds above.
MAX_OUTCOME_CHARS = 500
MAX_AREA_CHARS = 100
MAX_REPORTER_CHARS = 100
MAX_MEMBERS = 50
MAX_TOUCHES = 500
MAX_TOUCH_RESULT_CHARS = 2000


def _validate_contacts_value(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("enrichment.contacts value must be a list")
    if len(value) > MAX_CONTACTS:
        raise ValueError(f"enrichment.contacts has too many entries (max {MAX_CONTACTS})")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("enrichment.contacts entries must be objects")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("enrichment.contacts entry is missing 'name'")
        for key in ("phone", "email", "source"):
            if key in item and not isinstance(item[key], str):
                raise ValueError(f"enrichment.contacts.{key} must be a string")
        unknown = set(item) - {"name", "phone", "email", "source"}
        if unknown:
            raise ValueError(
                f"enrichment.contacts entry has unknown keys: {sorted(unknown)}")


def _validate_location_value(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("enrichment.location value must be an object")
    if not isinstance(value.get("text"), str) or not value["text"].strip():
        raise ValueError("enrichment.location is missing 'text'")
    if "maps_url" in value and not isinstance(value["maps_url"], str):
        raise ValueError("enrichment.location.maps_url must be a string")
    unknown = set(value) - {"text", "maps_url"}
    if unknown:
        raise ValueError(f"enrichment.location has unknown keys: {sorted(unknown)}")


def _validate_when_value(value: Any) -> None:
    # Chip 1 (board-tool-chat-parity-and-enrichment) already shipped ``when``
    # as a bare ISO string — ``card_stocking_identity``/``_when_day`` read
    # both shapes on purpose. D-BI3's ``{start, end?}`` is additive, not a
    # replacement: a bare string stays valid so existing stocked cards (and
    # the dedup fixtures built on that shape) keep validating.
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("enrichment.when must not be blank")
        return
    if not isinstance(value, dict):
        raise ValueError("enrichment.when value must be a string or an object")
    if not isinstance(value.get("start"), str) or not value["start"].strip():
        raise ValueError("enrichment.when is missing 'start'")
    if "end" in value and not isinstance(value["end"], str):
        raise ValueError("enrichment.when.end must be a string")
    unknown = set(value) - {"start", "end"}
    if unknown:
        raise ValueError(f"enrichment.when has unknown keys: {sorted(unknown)}")


def _validate_links_value(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("enrichment.links value must be a list")
    if len(value) > MAX_LINKS:
        raise ValueError(f"enrichment.links has too many entries (max {MAX_LINKS})")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("enrichment.links entries must be objects")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise ValueError("enrichment.links entry is missing 'label'")
        if not isinstance(item.get("url"), str) or not item["url"].strip():
            raise ValueError("enrichment.links entry is missing 'url'")
        unknown = set(item) - {"label", "url"}
        if unknown:
            raise ValueError(
                f"enrichment.links entry has unknown keys: {sorted(unknown)}")


#: field name -> validator of its ``value``. A field name absent here (the
#: chip-1 free-form facts, or a caller's own custom field) skips this check —
#: only these four names carry a shape contract.
_TYPED_ENRICHMENT_VALIDATORS = {
    "contacts": _validate_contacts_value,
    "location": _validate_location_value,
    "when": _validate_when_value,
    "links": _validate_links_value,
}


#: Guards every load-modify-save in this daemon process — the phone's taps
#: (``routes_board``) and the bot's tool calls (``board_bot_routes``) alike.
#: It lives HERE, next to the store it protects, because two route modules
#: holding two different locks is the same as holding none: a tap and a
#: ``board.add`` landing together would read the same board and one would
#: overwrite the other's card.
WRITE_LOCK = threading.Lock()


def _utcnow(now: "datetime | None" = None) -> str:
    """The event clock. Accepts an injected `now` so a caller that was GIVEN a
    clock can hand the same one to the event log — see `append_event`."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_bot_id(bot_id: str) -> str:
    """Reject anything that could traverse out of ``boards/``."""
    if not _BOT_ID_RE.match(bot_id or ""):
        raise ValueError(f"invalid bot id: {bot_id!r}")
    return bot_id


def board_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "boards" / validate_bot_id(bot_id)


def board_path(shared_dir: Path, bot_id: str) -> Path:
    return board_dir(shared_dir, bot_id) / "board.json"


def _adopt_store(shared_dir: Path, bot_id: str, *files: Path) -> None:
    """Give this bot's store (and the files just written into it) to the
    daemon user — a no-op unless we are running as root.

    Every writer here is reachable from ``sudo evolve-admin board …`` as well
    as from the daemon, and a root-written 0600 token hash is a hash the
    daemon cannot verify against. See ``board_store_perms`` for the incident.
    """
    adopt(Path(shared_dir) / "boards", board_dir(shared_dir, bot_id), *files)


def _empty_board(bot_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bot_id": bot_id,
        "updated_at": _utcnow(),
        "cards": [],
        "lists": [_personal_list_record()],
    }


def _personal_list_record() -> dict[str, Any]:
    """The list record every board carries by default (build item 0):
    ``{list_id, name, shape, members, id_prefix, defaults, next_seq}``. A
    fresh board (:func:`_empty_board`) is born with it; an older on-disk
    board gains it on load via :func:`ensure_personal_list`."""
    return {
        "list_id": DEFAULT_LIST_ID, "name": "Board", "shape": "assistant",
        "members": [{"id": "user", "display": "You"}],
        "id_prefix": None, "defaults": {}, "next_seq": 1,
        "created_at": _utcnow(),
    }


def find_list(board: dict[str, Any], list_id: str) -> dict[str, Any] | None:
    """The list record named ``list_id``, or None."""
    for lst in board.get("lists", []):
        if lst.get("list_id") == list_id:
            return lst
    return None


def ensure_personal_list(board: dict[str, Any]) -> bool:
    """Idempotent: adds the personal assistant list iff this board doesn't
    have one yet. Returns whether it changed anything — the same
    changed-or-not shape :func:`migrate_board` uses for its own
    conversions, and called from there on every load."""
    if find_list(board, DEFAULT_LIST_ID) is not None:
        return False
    board.setdefault("lists", []).insert(0, _personal_list_record())
    return True


def validate_enrichment(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize an ``enrichment{}`` block, or raise ``ValueError``.

    Shape (addendum §2): a flat map of field name → ``{value, source,
    captured_at}``. ``value`` is any JSON the caller has; ``source`` names
    where it came from ("calendar", "tmdb", "the user said so") and is
    REQUIRED — an unattributed fact is exactly what this block exists to stop.
    ``captured_at`` is stamped here when absent, because the one honest answer
    to "when was this true?" is the moment it was captured.

    Absent stays absent: ``None`` and ``{}`` both yield ``{}`` and the card
    carries no ``enrichment`` key at all.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("enrichment must be an object of {field: {value, source}}")
    if len(raw) > MAX_ENRICHMENT_FIELDS:
        raise ValueError(
            f"too many enrichment fields (max {MAX_ENRICHMENT_FIELDS})")
    out: dict[str, dict[str, Any]] = {}
    for name, field in raw.items():
        if not isinstance(name, str) or not _SLUG_RE.match(name):
            raise ValueError(f"invalid enrichment field name: {name!r}")
        if not isinstance(field, dict):
            raise ValueError(
                f"enrichment.{name} must be an object with 'value' and 'source'")
        unknown = set(field) - set(ENRICHMENT_FIELD_KEYS)
        if unknown:
            raise ValueError(
                f"enrichment.{name} has unknown keys: {sorted(unknown)}; "
                f"allowed: {list(ENRICHMENT_FIELD_KEYS)}")
        if "value" not in field:
            raise ValueError(f"enrichment.{name} is missing 'value'")
        source = field.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(
                f"enrichment.{name} is missing 'source' — every enrichment "
                "field must name where it came from")
        captured_at = field.get("captured_at")
        if captured_at is not None and not isinstance(captured_at, str):
            raise ValueError(f"enrichment.{name}.captured_at must be a string")
        typed_validator = _TYPED_ENRICHMENT_VALIDATORS.get(name)
        if typed_validator is not None:
            typed_validator(field["value"])
        out[name] = {
            "value": field["value"],
            "source": source.strip(),
            "captured_at": captured_at or _utcnow(),
        }
    size = len(json.dumps(out, ensure_ascii=False))
    if size > MAX_ENRICHMENT_CHARS:
        raise ValueError(
            f"enrichment is too large ({size} chars, max {MAX_ENRICHMENT_CHARS})")
    return out


def migrate_board(board: dict[str, Any]) -> bool:
    """Bring a loaded board up to the D-BI7/D-TM1 shape in memory. Returns
    whether anything changed.

    Also stamps the D-TM1 personal list onto a board that predates the
    Tracker (:func:`ensure_personal_list`) and ``list_id: "personal"`` onto
    any card that has none — additive, idempotent, in-memory only, same as
    every other conversion here.

    One conversion, applied on every load and idempotent:
    ``lane: bot`` → ``owner: bot`` + ``lane: today``, with a ``delegation``
    block stamped ``offered`` if the card had none (that lane WAS the offer).
    Every other card gains the explicit default ``owner: "me"``.

    Deliberately in-memory only — nothing is written here. ``load_board`` is
    called on every read (including the phone's 30 s poll), and a read path
    that writes is a read path that can fail on a full or read-only disk. The
    next write through :func:`save_board` persists the migrated shape; until
    then every reader still sees the new one, because they all come through
    this function.

    **Reverting.** Reverting the change that introduced this does NOT un-migrate
    a board that has already been saved: those cards keep ``owner`` and
    ``delegation`` and lose the lane that used to carry the same meaning, so
    handed-over work silently reappears as the operator's own Today cards. The
    inverse is exact and applies per card: ``owner == "bot"`` → ``lane: "bot"``,
    then drop ``owner``, and drop ``delegation`` when its state is ``offered``
    (that state IS the old lane). ``owner == "me"`` needs only ``owner`` dropped.
    **What going back cannot preserve:** a ``delegation`` past ``offered`` —
    ``accepted``, ``returned`` and anything after — is information the old
    single-lane shape could not hold, and it is lost. Written down because the
    migration is one-way in practice and the way back is otherwise inferable
    only from this function's body.
    """
    changed = ensure_personal_list(board)
    for card in board.get("cards", []):
        if not isinstance(card, dict):
            continue
        # D-TM1 build item 0: "existing cards migrate to the bot's personal
        # assistant list" — every card that predates the Tracker's list_id
        # field reads as belonging to it, same one-line idempotent stamp
        # the D-BI7 owner conversion below already uses as its pattern.
        if not card.get("list_id"):
            card["list_id"] = DEFAULT_LIST_ID
            changed = True
        if card.get("lane") == LEGACY_BOT_LANE:
            card["lane"] = "today"
            card["owner"] = "bot"
            if not card.get("delegation"):
                card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
            changed = True
        elif card.get("owner") not in OWNERS:
            card["owner"] = "me"
            changed = True
        # A card settled before ``settled_at`` existed has no honest answer to
        # "when was it settled?", so the migration records when it was first
        # OBSERVED settled: now. Not ``created_at`` — a card made 40 days ago
        # and finished last week would be stamped 40 days old and vanish from
        # the board on the first load after deploy, which is the one moment a
        # user is most likely to look for it. Failing toward a visible tile is
        # the same bias :func:`is_archived` already takes for an unparseable
        # stamp; the cost is that genuinely old settled cards linger 30 more
        # days once, and then age off normally.
        if card.get("lane") in SETTLED_LANES and not card.get("settled_at"):
            card["settled_at"] = _utcnow()
            changed = True
    return changed


def _parse_ts(value: Any) -> datetime | None:
    """One of this store's ``%Y-%m-%dT%H:%M:%SZ`` stamps, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def is_archived(card: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether this card's TILE has aged off the default board read (D-BI2).

    True only for a settled card (``done``/``dropped``) settled more than
    :data:`ARCHIVE_AFTER_DAYS` ago. Nothing is deleted and nothing is moved:
    an archived card is still in ``board.json``, still dedups stocking, and
    still comes back under ``?include=archived``. A settled card with an
    unparseable stamp is treated as NOT archived — the failure mode of a bad
    timestamp should be a visible tile, not a silently vanished one.
    """
    if card.get("lane") not in SETTLED_LANES:
        return False
    settled = _parse_ts(card.get("settled_at"))
    if settled is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - settled).days >= ARCHIVE_AFTER_DAYS


def visible_cards(
    board: dict[str, Any], *, include_archived: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The board's cards as a reader should see them — every card when
    ``include_archived``, otherwise everything that has not aged out."""
    cards = board.get("cards", [])
    if include_archived:
        return list(cards)
    return [c for c in cards if not is_archived(c, now=now)]


# ── stocking dedup (D-BI2) ────────────────────────────────────────────────
# "A dismissed calendar/email card never returns." The Morning Board run
# calls :func:`find_settled_duplicate` BEFORE adding; a hit means skip.

_IDENTITY_NOISE_RE = re.compile(r"[^a-z0-9]+")


def normalise_title(title: str) -> str:
    """Lowercased, punctuation- and spacing-insensitive form of a title.

    The fallback identity when a candidate has no source id: "Dentist — 2pm"
    and "dentist 2pm" are the same task arriving twice from two renderings of
    one calendar entry, and the user dismissed it once.
    """
    return _IDENTITY_NOISE_RE.sub(" ", (title or "").lower()).strip()


def _when_day(when: Any) -> str:
    """The DAY part of a when-ish value ("2026-09-12T09:00:00Z" → 2026-09-12).

    A day, not an instant: a recurring or rescheduled-by-minutes event is the
    same task on the same day, and an exact-timestamp match would let a
    one-minute shift resurrect a card the user dropped.
    """
    if isinstance(when, dict):
        when = when.get("start") or when.get("value")
    if not isinstance(when, str):
        return ""
    return when[:10]


def stocking_identity(
    *, title: str, source_id: str | None = None, when: Any = None,
) -> str:
    """The identity a stocking candidate dedups on.

    Prefers the upstream id (a calendar event id, an email message id) —
    exact, stable across re-renderings of the same thing. Falls back to
    normalised title (+ the day, when the candidate has one) so a source that
    hands out no id still cannot re-add what the user already settled.
    """
    sid = (source_id or "").strip()
    if sid:
        return "src:" + sid
    day = _when_day(when)
    return "t:" + normalise_title(title) + ("|" + day if day else "")


def card_stocking_identity(card: dict[str, Any]) -> str:
    """:func:`stocking_identity` for a card already on the board."""
    enrichment = card.get("enrichment") or {}
    when = enrichment.get("when", {}).get("value") if isinstance(
        enrichment.get("when"), dict) else None
    return stocking_identity(
        title=str(card.get("title") or ""),
        source_id=card.get("source_id"),
        when=when,
    )


def find_settled_duplicate(
    board: dict[str, Any], *, title: str, source_id: str | None = None,
    when: Any = None,
) -> dict[str, Any] | None:
    """The ``done``/``dropped`` card this candidate repeats, or None.

    **Archived cards count.** The 30-day tile filter is about what is worth
    looking at; this is about what the user already decided, and that does not
    expire — a card dropped last year must not come back this morning. So this
    scans ``board["cards"]`` directly rather than :func:`visible_cards`.

    Live cards (inbox/today/later) are deliberately NOT matched: a task still
    on the board is the caller's own duplicate-add problem, and silently
    skipping there would hide a genuine second occurrence of a recurring
    event the user has not dealt with yet.
    """
    wanted = stocking_identity(title=title, source_id=source_id, when=when)
    if wanted in ("t:", "src:"):
        return None
    for card in board.get("cards", []):
        if not isinstance(card, dict) or card.get("lane") not in SETTLED_LANES:
            continue
        if card_stocking_identity(card) == wanted:
            return card
    return None


#: Bots whose D-BI7 conversion has already been announced in this process.
#: ``migrate_board`` runs on EVERY load (including the phone's 30 s poll), so
#: without this the operator's log would carry the same line twice a minute.
#:
#: Once per PROCESS, not once ever: the conversion is in memory (a read path
#: that writes is one that can fail on a full or read-only disk — see
#: :func:`migrate_board`), so a board that is read but never written keeps the
#: retired lane on disk and announces itself again after a daemon restart.
#: Every reader still sees the converted shape in the meantime, because they
#: all come through :func:`load_board`. The log line says this rather than
#: claiming a one-time event it cannot deliver.
_MIGRATION_LOGGED: set[str] = set()


def load_board(shared_dir: Path, bot_id: str) -> dict[str, Any]:
    """The board, or an empty one when nothing has been written yet."""
    p = board_path(shared_dir, bot_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_board(bot_id)
    if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
        raise ValueError(f"corrupt board store: {p}")
    legacy = sum(
        1 for c in data["cards"]
        if isinstance(c, dict) and c.get("lane") == LEGACY_BOT_LANE)
    migrate_board(data)
    if legacy and bot_id not in _MIGRATION_LOGGED:
        _MIGRATION_LOGGED.add(bot_id)
        log.info(
            "board %s: D-BI7 migration — %d card(s) left the retired 'bot' "
            "lane and now read as owner=bot in 'today' (delegation "
            "preserved). Applied on every read; it reaches board.json on the "
            "next write to this board, and this line repeats after a daemon "
            "restart until then.", bot_id, legacy)
    return data


def save_board(shared_dir: Path, bot_id: str, board: dict[str, Any]) -> None:
    """Atomic temp+rename write, the same discipline as the arbiter stores."""
    d = board_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    board["updated_at"] = _utcnow()
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".board-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(board, f, indent=1, ensure_ascii=False)
        os.replace(tmp, board_path(shared_dir, bot_id))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _adopt_store(shared_dir, bot_id, board_path(shared_dir, bot_id))


def add_card(
    board: dict[str, Any],
    *,
    title: str,
    cluster: str,
    lane: str = "inbox",
    note: str = "",
    source: str = "manual",
    source_id: str | None = None,
    parent_id: str | None = None,
    owner: str = "me",
    enrichment: Any = None,
    list_id: str | None = None,
    outcome: str | None = None,
    assignee: str | None = None,
    reporter: str | None = None,
    severity: int | None = None,
    area: str | None = None,
    due: str | None = None,
    waiting_on: Any = None,
) -> dict[str, Any]:
    """Append a card and return it. Full-length ids: an id-keyed store with
    short random ids silently overwrites on collision, so these are 32 hex
    chars and checked against the board anyway.

    The trailing keyword args are D-TM2's Tracker fields — optional,
    creation-time only. Pace and the goal link are NOT here: those are
    given afterward via :func:`set_pace`/:func:`set_goal` (build item 5).
    """
    if lane not in LANES:
        raise ValueError(f"invalid lane: {lane!r}")
    if owner not in OWNERS:
        raise ValueError(f"invalid owner: {owner!r}")
    if owner == "other":
        waiting_on = _validate_waiting_on(waiting_on)
    elif waiting_on is not None:
        raise ValueError("waiting_on is only accepted when owner is 'other'")
    if not _SLUG_RE.match(cluster or ""):
        raise ValueError(f"invalid cluster: {cluster!r}")
    title = (title or "").strip()
    if not title:
        raise ValueError("card title is required")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"card title is too long (max {MAX_TITLE_CHARS} chars)")
    if len(note or "") > MAX_NOTE_CHARS:
        raise ValueError(f"card note is too long (max {MAX_NOTE_CHARS} chars)")
    if len(board["cards"]) >= MAX_CARDS:
        raise ValueError(f"board is full (max {MAX_CARDS} cards)")
    # D-TM1/2 (build items 0/1) — validated BEFORE the card is appended,
    # same discipline as the enrichment block below: a malformed Tracker
    # field must leave the board exactly as it was.
    list_id = list_id or DEFAULT_LIST_ID
    lst = find_list(board, list_id)
    if lst is None:
        raise ValueError(f"unknown list_id: {list_id!r}")
    outcome = _validate_outcome(outcome) if outcome is not None else None
    severity = _validate_severity(severity)
    area = _validate_area(area) if area is not None else None
    due = _validate_due(due)
    if assignee is not None:
        if lst.get("shape") != "project":
            raise ValueError(
                "assignee is only valid on a project-shape list")
        assignee = _validate_member_ref(board, list_id, assignee, "assignee")
    if reporter is not None:
        reporter = (reporter or "").strip()
        if len(reporter) > MAX_REPORTER_CHARS:
            raise ValueError(
                f"reporter is too long (max {MAX_REPORTER_CHARS} chars)")
    # Also validated here, ahead of :func:`mint_card_human_id`'s counter
    # mutation below: that mutation is not undone on a later raise, so
    # every OTHER validation (including this one, moved up from its old
    # spot next to the assignment) must run first.
    if source_id is not None and not isinstance(source_id, str):
        raise ValueError("source_id must be a string")
    source_id = (source_id or "").strip()
    if source_id and len(source_id) > MAX_TITLE_CHARS:
        raise ValueError(f"source_id is too long (max {MAX_TITLE_CHARS} chars)")
    existing = {c["id"] for c in board["cards"]}
    card_id = uuid.uuid4().hex
    while card_id in existing:  # pragma: no cover — 2^128 coincidence
        card_id = uuid.uuid4().hex
    # Validated BEFORE the card is appended: a malformed enrichment block must
    # leave the board exactly as it was, not add a card and then refuse.
    fields = validate_enrichment(enrichment)
    card: dict[str, Any] = {
        "id": card_id,
        "title": title,
        "note": note or "",
        "cluster": cluster,
        "lane": lane,
        "owner": owner,
        "source": source,
        "created_at": _utcnow(),
        "list_id": list_id,
    }
    # D-TM1 build item 0: a project list mints a human-quotable id
    # (``OP-0007``) from its own counter, mutating it — an assistant list's
    # cards carry no second id, the uuid is enough.
    human_id = mint_card_human_id(board, list_id)
    if human_id:
        card["human_id"] = human_id
    if outcome:
        card["outcome"] = outcome
    if owner == "other":
        card["waiting_on"] = waiting_on
    if assignee:
        card["assignee"] = assignee
    if reporter:
        card["reporter"] = reporter
    if severity is not None:
        card["severity"] = severity
    if area:
        card["area"] = area
    if due:
        card["due"] = due
    if parent_id:
        card["parent_id"] = parent_id
    # The upstream id this card came from (calendar event, email message).
    # Only ever read by the stocking dedup — it is what makes "never show me
    # this again" survive a re-render of the same event (D-BI2). Validated
    # above, before :func:`mint_card_human_id`'s counter mutation.
    if source_id:
        card["source_id"] = source_id
    if fields:
        card["enrichment"] = fields
    # D-BI5: the starter vocabulary is pure data, chosen from (source,
    # cluster) alone — no network read, no model call. Live cost/gating are
    # computed on READ (board_actions.bot_action_context/annotate_action),
    # never frozen here, so an integration granted after this card was
    # stocked immediately makes its actions usable.
    actions = board_actions.default_actions_for(cluster=cluster, source=source)
    if actions:
        card["actions"] = actions
    if owner == "bot":
        card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
    if lane in SETTLED_LANES:
        card["settled_at"] = card["created_at"]
    board["cards"].append(card)
    return card


def find_card(board: dict[str, Any], card_id: str) -> dict[str, Any] | None:
    for c in board["cards"]:
        if c.get("id") == card_id:
            return c
    return None


#: Shortest id PREFIX the store will resolve. Card ids are 32 hex chars —
#: right for a store that must never silently overwrite, wrong for a line the
#: model reads sixty of and for a human typing one into chat. Six hex chars is
#: 16.7M values against a ``MAX_CARDS`` of 5000, and an ambiguous prefix is an
#: error rather than a guess, so shortening the id can never move the wrong
#: card.
MIN_CARD_ID_PREFIX = 6


def resolve_card(board: dict[str, Any], ref: str) -> dict[str, Any]:
    """The card ``ref`` names — a full id, or an unambiguous id prefix.

    Raises ``KeyError`` when nothing matches and ``ValueError`` when a prefix
    matches more than one card (the caller must ask again with more of the id).
    """
    ref = (ref or "").strip()
    exact = find_card(board, ref)
    if exact is not None:
        return exact
    if len(ref) < MIN_CARD_ID_PREFIX:
        raise KeyError(ref)
    hits = [c for c in board["cards"] if str(c.get("id", "")).startswith(ref)]
    if not hits:
        raise KeyError(ref)
    if len(hits) > 1:
        raise ValueError(
            f"card id {ref!r} matches {len(hits)} cards — use more of the id")
    return hits[0]


#: The refusal ``move`` gives for the retired Bot lane. It names the verb that
#: DOES what the caller meant, because "invalid lane: 'bot'" is a true
#: sentence that leaves a model (or a person reading an error toast) with no
#: idea that hand-over still exists — it just moved to another axis (D-BI7).
BOT_LANE_REFUSAL = (
    "'bot' is not a lane — lanes answer WHEN (inbox/today/later/done/dropped) "
    "and who a card is on is the separate 'owner' attribute. To hand a card "
    "to the bot, assign it (owner='bot'); the card keeps its lane."
)


def move_card(
    shared_dir: Path, bot_id: str, card_id: str, to_lane: str, *, actor: str,
    reason: str | None = None, snooze_until: str | None = None,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """Move one card to a lane; append the triage event. Raises KeyError on
    an unknown card, ValueError on a bad lane or an ambiguous id prefix.

    ``now`` — same injected-clock discipline as :func:`record_touch`: a
    scheduled move (:mod:`board_touch`'s ``ask`` touch) or a test hands its
    own clock here rather than the wall clock.

    ``reason`` is the D-BI2 tap-reason and is accepted ONLY for a move to
    ``dropped`` — it is what the drop meant ("not mine", "never"), and a
    reason attached to a move into ``today`` would be a field the learning
    loop reads as a dismissal that never happened.

    ``snooze_until`` is the Stack's Later chip (D-ST4): a timestamp
    (``%Y-%m-%dT%H:%M:%SZ``) or bare date (``%Y-%m-%d``) the caller has
    already resolved from "tomorrow / this weekend / next week" — only the
    caller's clock and locale can do that, so this store never parses the
    preset itself, only the resolved value. Accepted ONLY for a move to
    ``later`` (the same restriction as ``reason``, and for the same
    reason — a snooze on any other lane is a field nothing reads). A
    ``later`` move that omits it clears any snooze the card already carried
    (a manual re-drag to Later — via the lane board's own sheet, which never
    sets this field — means "due now", not "still waiting").
    """
    if to_lane == LEGACY_BOT_LANE:
        raise ValueError(BOT_LANE_REFUSAL)
    if to_lane not in LANES:
        raise ValueError(f"invalid lane: {to_lane!r}")
    # A body is whatever JSON the caller sent. ``{"reason": ["never"]}`` used
    # to reach ``.strip()`` and raise AttributeError — a 500 for the same
    # class of mistake that gets a 400 one field over. Type-check first so
    # every malformed field refuses the same way (F4).
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"reason must be a string; one of {list(DROP_REASONS)}")
    reason = (reason or "").strip() or None
    if reason is not None:
        if to_lane != "dropped":
            raise ValueError(
                "a reason belongs to a drop — it is only accepted when "
                "moving a card to 'dropped'")
        if reason not in DROP_REASONS:
            raise ValueError(f"invalid reason; one of {list(DROP_REASONS)}")
    if snooze_until is not None and not isinstance(snooze_until, str):
        raise ValueError("snooze_until must be a string")
    snooze_until = (snooze_until or "").strip() or None
    if snooze_until is not None and to_lane != "later":
        raise ValueError(
            "snooze_until belongs to a Later move — it is only accepted "
            "when moving a card to 'later'")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    from_lane = card.get("lane")
    card["lane"] = to_lane
    # ``settled_at`` is what the 30-day tile filter ages against, so it is
    # stamped on the way IN to a settled lane and cleared on the way out —
    # a card pulled back out of Done is live again and must not carry a clock
    # that would archive it the moment it is re-finished.
    if to_lane in SETTLED_LANES:
        card["settled_at"] = _utcnow(now)
    else:
        card.pop("settled_at", None)
    if to_lane == "dropped":
        card["drop_reason"] = reason
        if reason is None:
            card.pop("drop_reason", None)
    else:
        card.pop("drop_reason", None)
    if to_lane == "later" and snooze_until is not None:
        card["snooze_until"] = snooze_until
    else:
        card.pop("snooze_until", None)
    # A move is a WHEN change and nothing else (D-BI7). Handing a card to the
    # bot is :func:`assign_card`, which is where the offer is made.
    save_board(shared_dir, bot_id, board)
    if to_lane == "dropped":
        # The learning loop's negative signal (D-BI2 / §5): one row that says
        # WHAT was refused and, when the user tapped one, why. A superset of
        # the ``triaged`` row it replaces — from/to are still here — so a
        # detector reading lane transitions loses nothing by this being its
        # own event name.
        #
        # ``actor`` IS LOAD-BEARING. The bot can drop a card too (board.move),
        # and that row is indistinguishable from the user's except by this
        # field. A detector reading "what this user never wants" must filter
        # on ``actor == "user"`` — counting the bot's own tidying would teach
        # the loop the bot's habits and call them the user's preferences.
        # Stated in design-pa-learning-loop-2026-08-31.md §1 as well, because
        # the detector will be written from that doc, not from here.
        append_event(shared_dir, bot_id, {
            "event": "dropped", "card": card_id, "title": card.get("title"),
            "from": from_lane, "to": to_lane, "reason": reason,
            "cluster": card.get("cluster"), "source": card.get("source"),
            "owner": card.get("owner") or "me", "actor": actor,
        }, now=now)
    else:
        event: dict[str, Any] = {
            "event": "triaged", "card": card_id, "title": card.get("title"),
            "from": from_lane, "to": to_lane, "actor": actor,
        }
        if to_lane == "later" and snooze_until is not None:
            event["snooze_until"] = snooze_until
        append_event(shared_dir, bot_id, event, now=now)
    return card


def split_card(
    shared_dir: Path, bot_id: str, card_id: str,
    *, user_part: str, bot_part: str, actor: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fork a card into linked siblings (D-PA4): the user part keeps the
    original's lane (or moves to today from inbox), the bot part shares that
    lane with ``owner: bot`` — an offer (D-BI7: two cards for two parts, not
    two lanes). The original is REPLACED by its children; the event row
    preserves its history."""
    user_part = (user_part or "").strip()
    bot_part = (bot_part or "").strip()
    if not user_part or not bot_part:
        raise ValueError("both user_part and bot_part are required")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    user_lane = str(card.get("lane") or "")
    if user_lane not in ("today", "later"):
        user_lane = "today"
    kid_user = add_card(board, title=user_part, cluster=card["cluster"],
                        lane=user_lane, note=card.get("note", ""),
                        source=card.get("source", "manual"), parent_id=card_id)
    # The bot's half is the same WHEN as the user's half — it differs by WHO
    # (D-BI7). ``owner="bot"`` is what stamps the delegation offer.
    kid_bot = add_card(board, title=bot_part, cluster=card["cluster"],
                       lane=user_lane, source=card.get("source", "manual"),
                       parent_id=card_id, owner="bot")
    board["cards"] = [c for c in board["cards"] if c.get("id") != card_id]
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "split", "card": card_id, "title": card.get("title"),
        "into": [kid_user["id"], kid_bot["id"]], "actor": actor,
    })
    return kid_user, kid_bot


def create_card(
    shared_dir: Path, bot_id: str,
    *, title: str, cluster: str, lane: str = "inbox", note: str = "",
    source: str = "manual", source_id: str | None = None,
    actor: str = "user", owner: str = "me",
    enrichment: Any = None,
    list_id: str | None = None, outcome: str | None = None,
    assignee: str | None = None, reporter: str | None = None,
    severity: int | None = None, area: str | None = None,
    due: str | None = None, waiting_on: Any = None,
) -> dict[str, Any]:
    """Add one card and log its stocking event."""
    board = load_board(shared_dir, bot_id)
    card = add_card(board, title=title, cluster=cluster, lane=lane,
                    note=note, source=source, source_id=source_id, owner=owner,
                    enrichment=enrichment, list_id=list_id, outcome=outcome,
                    assignee=assignee, reporter=reporter, severity=severity,
                    area=area, due=due, waiting_on=waiting_on)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "stocked", "card": card["id"], "title": card["title"],
        "cluster": cluster, "source": source, "owner": owner,
        "enriched": sorted(card.get("enrichment") or {}), "actor": actor,
    })
    return card


def assign_card(
    shared_dir: Path, bot_id: str, card_id: str, owner: str, *, actor: str,
    waiting_on: Any = None,
) -> dict[str, Any]:
    """Set a card's OWNER (D-BI7, widened by D-TM7) and log the hand-over.

    ``owner="bot"`` is an OFFER, not an instruction: it stamps
    ``delegation.state = "offered"`` and the bot accepts or declines from
    there (D-BI4). Assigning back to ``me`` clears the delegation block —
    a card nobody handed over has no delegation to report on.
    ``owner="other"`` (D-TM7) REQUIRES ``waiting_on {who, since, source}``
    and clears any ``delegation``/``instructed`` state the same way ``me``
    does — waiting on a third party is not a hand-over to the bot.

    Idempotent for ``me``/``bot``: re-assigning a card to the owner it
    already has is a no-op that still returns the card, so a double-tap on
    the phone and a retried tool call both land on the same state. NOT
    idempotent for ``other`` — a repeat call is how the wait's ``who``/
    ``source`` gets corrected or refreshed, so it always applies.
    """
    if owner not in OWNERS:
        raise ValueError(f"invalid owner; one of {list(OWNERS)}")
    if owner == "other":
        waiting_on = _validate_waiting_on(waiting_on)
    elif waiting_on is not None:
        raise ValueError("waiting_on is only accepted when owner is 'other'")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    from_owner = card.get("owner") or "me"
    if from_owner == owner and owner != "other":
        return card
    card["owner"] = owner
    if owner == "bot":
        card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
        card.pop("waiting_on", None)
    elif owner == "other":
        card["waiting_on"] = waiting_on
        card.pop("delegation", None)
        card.pop("instructed", None)
    else:
        card.pop("delegation", None)
        card.pop("waiting_on", None)
        # Taking a card back clears its last-tapped-action marker too — a
        # card nobody handed over has no "queued for the bot" state to show
        # (mirrors clearing ``delegation`` on the same transition).
        card.pop("instructed", None)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "assigned", "card": card_id, "title": card.get("title"),
        "from": from_owner, "to": owner, "actor": actor,
    })
    return card


def instruct_card(
    shared_dir: Path, bot_id: str, card_id: str, *,
    action_id: str, action_label: str, est_cost: float | None, actor: str,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """D-BI5: a tap is an instruction, not a chat.

    The caller (``routes_board``) has already resolved the action against the
    card's ``actions[]`` and recomputed whether it is CURRENTLY enabled
    (:mod:`board_actions`) — this writer trusts that and does the state
    change: owner becomes ``bot`` with a fresh ``offered`` delegation if the
    card was the user's (a tap on a bot-owned card's action is a further
    instruction on a hand-over that already happened, so it does not re-offer
    or re-log an assignment), a small ``instructed`` marker lands on the card
    so the tile can show "queued — est $X" without a second fetch, and the
    ``instruction`` event is appended to the per-bot log — the same log every
    other board writer appends to, and what a future event-driven worker
    (the next chip, D-BI4) will read.

    ``now`` — same injected-clock discipline as :func:`record_touch`: a
    scheduled tap (:mod:`board_touch`'s ``do_action`` touch) or a test hands
    its own clock here.

    Raises ``KeyError`` on an unknown card.
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if (card.get("owner") or "me") != "bot":
        card["owner"] = "bot"
        card["delegation"] = {"state": "offered", "updated_at": _utcnow(now)}
    card["instructed"] = {
        "action_id": action_id, "label": action_label,
        "est_cost": est_cost, "ts": _utcnow(now),
    }
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "instruction", "card": card_id, "title": card.get("title"),
        "action_id": action_id, "action_label": action_label,
        "est_cost": est_cost, "actor": actor,
    }, now=now)
    return card


def record_instruction_none(
    shared_dir: Path, bot_id: str, card_id: str, *, actor: str,
) -> dict[str, Any]:
    """D-BI5 §6 Q3's learning signal: none of the offered actions fit.

    No card mutation — this is purely a negative signal for widening the
    starter vocabulary, the same role :func:`move_card`'s drop-reason plays
    for lanes. Raises ``KeyError`` on an unknown card.
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    append_event(shared_dir, bot_id, {
        "event": "instruction_none", "card": card["id"],
        "title": card.get("title"), "actor": actor,
    })
    return card


def add_card_action(
    shared_dir: Path, bot_id: str, card_id: str, action: dict[str, Any], *,
    actor: str,
) -> dict[str, Any]:
    """Append one D-BI5 action to an EXISTING card's ``actions[]`` — the
    runtime counterpart to :func:`board_actions.default_actions_for`'s
    add-time computation (D-TM7's drafted nudge is the first caller: a
    ``check_source`` touch on an ``owner: other`` card earns the user a
    priced, rung-gated nudge action, not an automatic send).

    Idempotent by ``action["id"]``: replaces an action already on the card
    with the same id (a re-drafted nudge refreshes its own row) rather than
    growing a duplicate. Capped at
    :data:`board_actions.MAX_ACTIONS_PER_CARD`, the same starter-vocabulary
    ceiling ``add_card`` already enforces — at capacity, the OLDEST action
    is evicted to make room, never the new one silently dropped: this is a
    daemon-driven write for something that just happened (D-TM7's nudge is
    the first caller), which outranks a stocked default nobody has acted
    on since the card was added.
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    actions = card.setdefault("actions", [])
    actions[:] = [a for a in actions if a.get("id") != action.get("id")]
    while len(actions) >= board_actions.MAX_ACTIONS_PER_CARD:
        actions.pop(0)
    actions.append(action)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "action_added", "card": card_id, "title": card.get("title"),
        "action_id": action.get("id"), "label": action.get("label"), "actor": actor,
    })
    return card


def set_delegation_progress(
    shared_dir: Path, bot_id: str, card_id: str, *, state: str,
    note: str = "", cost_to_date: float | None = None, actor: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one delegation transition on a card the bot owns.

    Refuses a card whose owner is not the bot: progress is a report on a
    hand-over, and there is no hand-over to report on until someone made one.
    That refusal is the reason this is a separate writer rather than a field
    on :func:`move_card` — it is the one place the delegation lifecycle is
    enforced.

    ``result`` (board-bot-lane-worker, D-BI4 §4) is the terminal payload a
    ``done``/``returned_for_review`` transition carries — ``{text, cost,
    artifacts?}``. Accepted at any state (not just those two) so a caller
    never has to special-case which transition may carry one; nothing reads
    it before a terminal state anyway. Text is capped the same as a note —
    this is a result summary, not a document store.
    """
    if state not in DELEGATION_STATES:
        raise ValueError(f"invalid delegation state; one of {list(DELEGATION_STATES)}")
    if len(note or "") > MAX_NOTE_CHARS:
        raise ValueError(f"progress note is too long (max {MAX_NOTE_CHARS} chars)")
    if cost_to_date is not None:
        try:
            cost_to_date = float(cost_to_date)
        except (TypeError, ValueError):
            raise ValueError("cost_to_date must be a number") from None
        if cost_to_date < 0:
            raise ValueError("cost_to_date must not be negative")
    if result is not None:
        if not isinstance(result, dict) or not isinstance(result.get("text", ""), str):
            raise ValueError("result must be an object with a string 'text'")
        if len(result.get("text", "")) > MAX_NOTE_CHARS:
            raise ValueError(f"result.text is too long (max {MAX_NOTE_CHARS} chars)")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if (card.get("owner") or "me") != "bot":
        raise ValueError(
            "that card is not assigned to the bot — assign it first")
    prev = card.get("delegation") or {}
    delegation = {
        "state": state,
        "progress_note": (note or "").strip() or prev.get("progress_note", ""),
        "updated_at": _utcnow(),
    }
    if cost_to_date is not None:
        delegation["cost_to_date"] = cost_to_date
    elif "cost_to_date" in prev:
        delegation["cost_to_date"] = prev["cost_to_date"]
    if result is not None:
        delegation["result"] = result
    elif "result" in prev:
        delegation["result"] = prev["result"]
    card["delegation"] = delegation
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "delegation", "card": card_id, "title": card.get("title"),
        "from": prev.get("state"), "to": state,
        "note": delegation["progress_note"],
        "cost_to_date": delegation.get("cost_to_date"), "actor": actor,
    })
    return card


def record_approval_request(
    shared_dir: Path, bot_id: str, card_id: str, *,
    act: str, payload_summary: str, est_cost: float | None, actor: str,
) -> dict[str, Any]:
    """Land an externally-visible act the bot wants to take as an approval
    request on the card (board-bot-lane-worker §5). Refuses a card the bot
    does not own, same as :func:`set_delegation_progress`.

    ``pending_approval`` is a small marker (mirrors ``instructed``) so the
    phone sheet can render Approve/Decline without a second fetch. Cleared by
    :func:`record_approval_decision`, whichever way that goes.
    """
    if len(payload_summary or "") > MAX_NOTE_CHARS:
        raise ValueError(f"payload_summary is too long (max {MAX_NOTE_CHARS} chars)")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if (card.get("owner") or "me") != "bot":
        raise ValueError(
            "that card is not assigned to the bot — assign it first")
    card["pending_approval"] = {
        "act": act, "payload_summary": payload_summary,
        "est_cost": est_cost, "requested_at": _utcnow(),
    }
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "approval_requested", "card": card_id, "title": card.get("title"),
        "act": act, "payload_summary": payload_summary, "est_cost": est_cost,
        "actor": actor,
    })
    return card


def record_approval_decision(
    shared_dir: Path, bot_id: str, card_id: str, *, granted: bool, actor: str,
) -> dict[str, Any]:
    """Resolve a pending approval — granted or declined — and clear the
    marker. Raises ``ValueError`` if the card has no pending approval to
    resolve (nothing for a stray tap to act on).

    ``actor`` here is deliberately the CALLER's identity, not stamped to the
    bot — see :func:`move_card`'s note on why ``actor`` is load-bearing.
    Approving or declining is a USER decision about the bot's proposal; a
    detector reading "what this user approved" must see ``actor == "user"``,
    the same way the drop-reason detector must see it on a human drop.
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    pending = card.get("pending_approval")
    if not pending:
        raise ValueError("that card has no pending approval to resolve")
    card.pop("pending_approval", None)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "approval_granted" if granted else "approval_declined",
        "card": card_id, "title": card.get("title"),
        "act": pending.get("act"), "actor": actor,
    })
    return card


# ── Tracker: lists, outcome, owner, pace (D-TM1/2/5/8) ──────────────────────
# design-pa-tasks-and-follow-through-2026-09-18.md. Schema and a handful of
# pure-function/store-API pieces only — nothing here fires a touch on a
# clock, nothing here calls a model (guardrails). Every field is optional
# and an old card loads unchanged (:func:`migrate_board` stamps only
# ``list_id``, and only when the card has none).

def _validate_waiting_on(raw: Any) -> dict[str, Any]:
    """``owner: "other"``'s required detail — see :data:`WAITING_ON_KEYS`."""
    if not isinstance(raw, dict):
        raise ValueError(
            "owner 'other' requires waiting_on {who, since, source}")
    unknown = set(raw) - set(WAITING_ON_KEYS)
    if unknown:
        raise ValueError(f"waiting_on has unknown keys: {sorted(unknown)}")
    who = raw.get("who")
    if not isinstance(who, str) or not who.strip():
        raise ValueError("waiting_on is missing 'who'")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("waiting_on is missing 'source'")
    since = raw.get("since")
    if since is not None and not isinstance(since, str):
        raise ValueError("waiting_on.since must be a string")
    return {"who": who.strip(), "source": source.strip(),
            "since": since or _utcnow()}


def _parse_date(value: Any) -> "datetime | None":
    """A bare ``YYYY-MM-DD`` (the ``due`` shape) — distinct from
    :func:`_parse_ts`'s full timestamp. Unparseable or absent -> None.
    Returns a naive ``datetime`` at midnight (not a ``date``) so callers can
    combine it with a time-of-day without a second conversion."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _validate_due(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _parse_date(value) is None:
        raise ValueError("due must be a 'YYYY-MM-DD' date string")
    return value


def _validate_severity(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or value not in SEVERITIES:
        raise ValueError(f"invalid severity; one of {list(SEVERITIES)}")
    return value


def _validate_cadence(value: Any) -> str | None:
    if value is None:
        return None
    if value not in CADENCE_CLASSES:
        raise ValueError(f"invalid cadence; one of {list(CADENCE_CLASSES)}")
    return value


def _validate_touch_action(value: Any) -> str | None:
    if value is None:
        return None
    if value not in TOUCH_ACTIONS:
        raise ValueError(f"invalid touch_action; one of {list(TOUCH_ACTIONS)}")
    return value


def _validate_outcome(value: Any) -> str:
    outcome = (value or "").strip()
    if len(outcome) > MAX_OUTCOME_CHARS:
        raise ValueError(f"outcome is too long (max {MAX_OUTCOME_CHARS} chars)")
    return outcome


def _validate_area(value: Any) -> str:
    area = (value or "").strip()
    if len(area) > MAX_AREA_CHARS:
        raise ValueError(f"area is too long (max {MAX_AREA_CHARS} chars)")
    return area


def _validate_pace_invariant(card: dict[str, Any]) -> None:
    """"a card with next_touch but no touch_action is a schema error at
    write" (build item 1) — the one cross-field rule this schema enforces,
    checked at every writer that can touch either field."""
    if card.get("next_touch") and not card.get("touch_action"):
        raise ValueError("a card with next_touch must also carry touch_action")


# ── lists (build item 0) ─────────────────────────────────────────────────

def _validate_members(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("members must be a list")
    if len(raw) > MAX_MEMBERS:
        raise ValueError(f"too many members (max {MAX_MEMBERS})")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("member entries must be objects")
        unknown = set(item) - {"id", "display", "slack_user"}
        if unknown:
            raise ValueError(f"member has unknown keys: {sorted(unknown)}")
        mid = item.get("id")
        if not isinstance(mid, str) or not _MEMBER_ID_RE.match(mid):
            raise ValueError(f"invalid member id: {mid!r}")
        if mid in seen:
            raise ValueError(f"duplicate member id: {mid!r}")
        seen.add(mid)
        display = item.get("display")
        if not isinstance(display, str) or not display.strip():
            raise ValueError("member is missing 'display'")
        member = {"id": mid, "display": display.strip()}
        if "slack_user" in item:
            su = item["slack_user"]
            if not isinstance(su, str) or not su.strip():
                raise ValueError("member.slack_user must be a non-empty string")
            member["slack_user"] = su.strip()
        out.append(member)
    return out


def _validate_list_defaults(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("list defaults must be an object")
    unknown = set(raw) - {"cadence", "nag_days"}
    if unknown:
        raise ValueError(f"list defaults has unknown keys: {sorted(unknown)}")
    out: dict[str, Any] = {}
    if "cadence" in raw:
        out["cadence"] = _validate_cadence(raw["cadence"])
    if "nag_days" in raw:
        nag = raw["nag_days"]
        if nag is not None and (isinstance(nag, bool) or not isinstance(nag, int) or nag <= 0):
            raise ValueError("nag_days must be a positive integer")
        out["nag_days"] = nag
    return out


def _slugify_list_id(name: str, *, existing: set[str]) -> str:
    base = _IDENTITY_NOISE_RE.sub("-", (name or "").lower()).strip("-")[:40] or "list"
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def create_list(
    shared_dir: Path, bot_id: str, *, name: str, shape: str,
    members: Any = None, id_prefix: str | None = None, defaults: Any = None,
    actor: str,
) -> dict[str, Any]:
    """Mint a new list on this bot's board (build item 0). Daemon-only by
    construction: no route in this chip calls it. ``project`` lists require
    an ``id_prefix`` (2-6 uppercase letters, e.g. ``OP``/``GD``) — what
    :func:`mint_card_human_id` mints against; ``assistant`` lists must not
    carry one."""
    if shape not in LIST_SHAPES:
        raise ValueError(f"invalid list shape; one of {list(LIST_SHAPES)}")
    name = (name or "").strip()
    if not name:
        raise ValueError("list name is required")
    if len(name) > MAX_TITLE_CHARS:
        raise ValueError(f"list name is too long (max {MAX_TITLE_CHARS} chars)")
    if shape == "project":
        if not id_prefix or not _ID_PREFIX_RE.match(id_prefix):
            raise ValueError(
                "project lists require an id_prefix of 2-6 uppercase letters")
    elif id_prefix:
        raise ValueError("id_prefix is only for project-shape lists")
    members_v = _validate_members(members)
    defaults_v = _validate_list_defaults(defaults)
    board = load_board(shared_dir, bot_id)
    existing = {lst["list_id"] for lst in board.get("lists", [])}
    list_id = _slugify_list_id(name, existing=existing)
    record = {
        "list_id": list_id, "name": name, "shape": shape,
        "members": members_v, "id_prefix": id_prefix, "defaults": defaults_v,
        "next_seq": 1, "created_at": _utcnow(),
    }
    board.setdefault("lists", []).append(record)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "list_created", "list_id": list_id, "name": name,
        "shape": shape, "actor": actor,
    })
    return record


def mint_card_human_id(board: dict[str, Any], list_id: str) -> str | None:
    """<prefix>-NNNN for a project list's next card, mutating its counter —
    None for an assistant list or an unresolved list_id (those cards carry
    no human id; the uuid is enough). The counter only ever increments — a
    settled, or even a never-saved, card's number is never reused."""
    lst = find_list(board, list_id)
    if lst is None or lst.get("shape") != "project" or not lst.get("id_prefix"):
        return None
    seq = int(lst.get("next_seq") or 1)
    lst["next_seq"] = seq + 1
    return f"{lst['id_prefix']}-{seq:04d}"


def _validate_member_ref(
    board: dict[str, Any], list_id: str, member_id: str, field: str,
) -> str:
    lst = find_list(board, list_id)
    if lst is None:
        raise ValueError(f"unknown list_id: {list_id!r}")
    ids = {m["id"] for m in lst.get("members", [])}
    if member_id not in ids:
        raise ValueError(
            f"{field} {member_id!r} is not a member of list {list_id!r}")
    return member_id


# ── pace (D-TM5) ──────────────────────────────────────────────────────────

#: First-touch offset per class, and the backed-off gap it can never exceed
#: (build item 2). ``"today"`` is the one wall-clock target (18:00 in the
#: caller's ``tz`` — see :func:`next_touch_for`); every other class is a
#: plain offset from ``now``. Caps are this chip's own call (the brief
#: specifies the five first-touch offsets, not the caps): wide enough that
#: back-off has room to work, narrow enough that a class still means
#: something — "someday" backed off past four months is just "later" with
#: extra steps.
CADENCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "now":     {"first_touch": timedelta(minutes=30), "cap": timedelta(hours=6)},
    "today":   {"first_touch": "18:00",                "cap": timedelta(days=3)},
    "soon":    {"first_touch": timedelta(days=2),       "cap": timedelta(days=14)},
    "later":   {"first_touch": timedelta(days=7),       "cap": timedelta(days=30)},
    "someday": {"first_touch": timedelta(days=30),      "cap": timedelta(days=120)},
}

#: D-TM5: "a touch whose result is 'no change / still waiting' backs the
#: next one off x1.5". A fixed vocabulary, not free text, for the same
#: reason :data:`DROP_REASONS` is one: a detector can only widen a set it
#: can enumerate.
BACKOFF_FACTOR = 1.5
NO_MOVEMENT_RESULTS = ("no_change", "still_waiting")

#: D-TM3's touch_action -> a short label. Reused by the BOARD.md mirror and
#: by :func:`board_stack.next_touch_line`'s Stack-face summary.
TOUCH_ACTION_LABELS = {
    "remind": "remind", "check_source": "check the source",
    "do_action": "do it", "ask": "ask",
}


def next_touch_for(
    card: dict[str, Any], now: "datetime | None" = None, *, tz: Any = None,
) -> str | None:
    """D-TM5's table, applied to one card (build item 2). Pure: no clock
    read when ``now`` is given, no store access, no write. Returns an ISO
    UTC stamp or None when the card has neither ``cadence`` nor ``due``.

    ``due`` overrides the cadence: the touch lands the day before, 09:00 in
    ``tz`` (default UTC — no per-bot zone is wired here yet; that is the
    touch-scheduler chip's job), or now if that instant already passed.
    Otherwise the class's first-touch offset applies from ``now``, backed
    off x1.5 per touch in a TRAILING run of :data:`NO_MOVEMENT_RESULTS`
    (newest first, stopping at the first touch that moved something),
    capped at the class's ceiling.
    """
    now = now or datetime.now(timezone.utc)
    tz = tz or timezone.utc
    due = _parse_date(card.get("due"))
    if due is not None:
        day_before = (due - timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0, tzinfo=tz)
        day_before_utc = day_before.astimezone(timezone.utc)
        return _utcnow(now if day_before_utc <= now else day_before_utc)
    cadence = card.get("cadence")
    if not isinstance(cadence, str):
        return None
    spec = CADENCE_DEFAULTS.get(cadence)
    if spec is None:
        return None
    if spec["first_touch"] == "18:00":
        local_today = now.astimezone(tz).replace(
            hour=18, minute=0, second=0, microsecond=0)
        target_utc = local_today.astimezone(timezone.utc)
        if target_utc <= now:
            target_utc += timedelta(days=1)
        offset = target_utc - now
    else:
        offset = spec["first_touch"]
    streak = 0
    for touch in reversed(card.get("touches") or []):
        if not isinstance(touch, dict) or touch.get("result") not in NO_MOVEMENT_RESULTS:
            break
        streak += 1
    offset = min(offset * (BACKOFF_FACTOR ** streak), spec["cap"])
    return _utcnow(now + offset)


def set_pace(
    shared_dir: Path, bot_id: str, card_id: str, *,
    cadence: str | None = None, next_touch: str | None = None,
    touch_action: str | None = None, actor: str, tz: Any = None,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """D-TM2/5's pace setter (build item 5) — the only way a card's
    ``cadence``/``next_touch``/``touch_action`` change once it exists.
    All three ``None`` CLEARS the pace. An explicit ``next_touch`` is used
    verbatim (the caller already resolved it — a user's "nudge me sooner");
    omitting it while ``cadence`` is given computes one via
    :func:`next_touch_for`. Raises ``ValueError`` for the D-TM2 schema
    error (``next_touch`` with no ``touch_action``) or an unknown value.

    ``tz`` is the pod timezone (e.g. from ``config.resolve_pod_timezone``)
    for the "today" class's 18:00-local target and a ``due`` date's
    day-before-09:00-local touch — the ``schedule.touch(card, when, zone)``
    verb named in ``design-application-platform-2026-09-22.md`` §4.
    Defaults to UTC, matching every caller before this chip.

    ``now`` is the same injected-clock discipline :func:`append_event`
    already follows: a caller that was handed a clock (the touch scheduler,
    a test, a replay) hands the SAME one here so the computed
    ``next_touch`` and the event row describing this call land on one
    consistent timeline. Defaults to the wall clock, unchanged for every
    caller before this chip.
    """
    cadence = _validate_cadence(cadence)
    touch_action = _validate_touch_action(touch_action)
    if next_touch is not None and not isinstance(next_touch, str):
        raise ValueError("next_touch must be a string")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if cadence is None and next_touch is None and touch_action is None:
        card.pop("cadence", None)
        card.pop("next_touch", None)
        card.pop("touch_action", None)
    else:
        if cadence is not None:
            card["cadence"] = cadence
        else:
            card.pop("cadence", None)
        if touch_action is not None:
            card["touch_action"] = touch_action
        if next_touch is not None:
            card["next_touch"] = next_touch
        elif cadence is not None:
            computed = next_touch_for(card, now, tz=tz)
            if computed is not None:
                card["next_touch"] = computed
            else:
                card.pop("next_touch", None)
        _validate_pace_invariant(card)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "pace_set", "card": card_id, "title": card.get("title"),
        "cadence": card.get("cadence"), "next_touch": card.get("next_touch"),
        "touch_action": card.get("touch_action"), "actor": actor,
    }, now=now)
    return card


def record_touch(
    shared_dir: Path, bot_id: str, card_id: str, *,
    action: str, result: str = "", actor: str, tz: Any = None,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """D-TM3's log (build item 5): append one ``{at, action, result,
    actor}`` row and recompute ``next_touch`` from the table. "No touch
    fires here" (guardrail) means nothing calls this on a clock; it is the
    entry point a human, a test, or a future scheduler uses to RECORD a
    touch that already happened.

    ``tz`` — see :func:`set_pace`; threaded to the ``next_touch`` recompute
    so a fired touch reschedules in the pod's own zone, not UTC.

    ``now`` — same injected-clock discipline as :func:`set_pace`: the touch
    scheduler (:mod:`board_touch`) hands its own tick's clock here so the
    ``at``/``next_touch`` this call writes and the ``touch_due`` event that
    triggered it land on one timeline, not two clocks that can disagree.
    Defaults to the wall clock.
    """
    if action not in TOUCH_ACTIONS:
        raise ValueError(f"invalid touch action; one of {list(TOUCH_ACTIONS)}")
    result = (result or "").strip()
    if len(result) > MAX_TOUCH_RESULT_CHARS:
        raise ValueError(
            f"touch result is too long (max {MAX_TOUCH_RESULT_CHARS} chars)")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    touches = card.setdefault("touches", [])
    if len(touches) >= MAX_TOUCHES:
        raise ValueError(f"card has too many touches (max {MAX_TOUCHES})")
    touches.append({"at": _utcnow(now), "action": action, "result": result,
                    "actor": actor})
    card["touch_action"] = action
    computed = next_touch_for(card, now, tz=tz)
    if computed is not None:
        card["next_touch"] = computed
    else:
        card.pop("next_touch", None)
        card.pop("touch_action", None)
    _validate_pace_invariant(card)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "touch", "card": card_id, "title": card.get("title"),
        "action": action, "result": result,
        "next_touch": card.get("next_touch"), "actor": actor,
    }, now=now)
    return card


def set_goal(
    shared_dir: Path, bot_id: str, card_id: str, goal_id: str | None, *,
    actor: str,
) -> dict[str, Any]:
    """D-TM8: link (or clear, with ``goal_id=None``) a card's goal — one
    level deep, always.

    The refusal is checked against the GOAL's OWN ``belong_to``: a goal
    that itself belongs to something is refused outright (chaining through
    it would make a 3-card line) — "a card whose belong_to target itself
    has belong_to is refused", verbatim (build item 1).
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if not goal_id:
        card.pop(GOAL_BELONG_TO_KEY, None)
    else:
        if goal_id == card_id:
            raise ValueError("a card cannot be its own goal")
        goal = resolve_card(board, goal_id)
        if goal.get(GOAL_BELONG_TO_KEY):
            raise ValueError(
                "that card already belongs to a goal — goals are one level "
                "deep, so it cannot itself be used as a goal")
        card[GOAL_BELONG_TO_KEY] = goal["id"]
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "goal_set", "card": card_id, "title": card.get("title"),
        "goal": card.get(GOAL_BELONG_TO_KEY), "actor": actor,
    })
    return card


def briefing_path(shared_dir: Path, bot_id: str) -> Path:
    return board_dir(shared_dir, bot_id) / "briefing.json"


def _write_briefing(shared_dir: Path, bot_id: str, briefing: dict[str, Any]) -> None:
    d = board_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".briefing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(briefing, f, indent=1, ensure_ascii=False)
        os.replace(tmp, briefing_path(shared_dir, bot_id))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _adopt_store(shared_dir, bot_id, briefing_path(shared_dir, bot_id))


def load_briefing(shared_dir: Path, bot_id: str) -> dict[str, Any] | None:
    """Today's (or whatever day was last composed) briefing panel record, or
    None when the Morning Board app has never stocked this bot."""
    try:
        data = json.loads(briefing_path(shared_dir, bot_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def save_briefing(
    shared_dir: Path, bot_id: str, text: str, *, date: str | None = None,
) -> dict[str, Any]:
    """Store the composed morning message as the board page's collapsible
    panel (D-BI6d/e): ``the briefing is the same composition delivered
    twice`` — this is never re-composed from the panel side, only handed the
    text the daily message already carries.

    A same-day call (e.g. a forced re-run) preserves whatever
    ``dismissed_at`` the day already recorded — a resend must not silently
    un-dismiss a panel the user already closed. A new day always starts
    undismissed.
    """
    validate_bot_id(bot_id)
    date = date or _utcnow()[:10]
    text = (text or "").strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ValueError(f"briefing text is too long (max {MAX_NOTE_CHARS} chars)")
    existing = load_briefing(shared_dir, bot_id)
    dismissed_at = (existing.get("dismissed_at")
                    if existing and existing.get("date") == date else None)
    briefing = {"date": date, "text": text, "created_at": _utcnow(),
                "dismissed_at": dismissed_at}
    _write_briefing(shared_dir, bot_id, briefing)
    return briefing


def dismiss_briefing(shared_dir: Path, bot_id: str, *, date: str | None = None) -> dict[str, Any]:
    """Mark the panel dismissed for ``date`` (default today).

    Raises ``KeyError`` when no briefing is on file for that day — a stale
    dismiss tap on a panel that already rotated to a new day is a no-op the
    caller should swallow, never a write that resurrects an old record.
    """
    date = date or _utcnow()[:10]
    existing = load_briefing(shared_dir, bot_id)
    if not existing or existing.get("date") != date:
        raise KeyError(date)
    existing["dismissed_at"] = _utcnow()
    _write_briefing(shared_dir, bot_id, existing)
    return existing


def append_event(shared_dir: Path, bot_id: str, event: dict[str, Any],
                 *, now: "datetime | None" = None) -> None:
    """One JSONL row per interaction — the learning loop's raw material.

    `now` exists because the day file is named from the event's own
    timestamp. A caller that was handed a clock (a scheduled run, a test,
    a replay) must be able to hand the SAME clock here, or the card it
    just wrote and the event row describing that write land on two
    different days. That split is invisible until the two clocks disagree,
    which is why it survived until a wall clock drifted past a fixture.
    """
    d = board_dir(shared_dir, bot_id) / "events"
    d.mkdir(parents=True, exist_ok=True)
    event = {"ts": _utcnow(now), **event}
    day = event["ts"][:10]
    with (d / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    _adopt_store(shared_dir, bot_id, d, d / f"{day}.jsonl")


# ── token ──────────────────────────────────────────────────────────────────

def _token_path(shared_dir: Path, bot_id: str) -> Path:
    return board_dir(shared_dir, bot_id) / "token.sha256"


#: Bots whose unreadable token hash has already been reported, so a phone
#: polling every 30s produces one line and not a log flood. Per process —
#: the daemon restarts on deploy, which is exactly when the state may have
#: been repaired and is worth saying again.
_UNREADABLE_WARNED: set[str] = set()
_UNREADABLE_LOCK = threading.Lock()


def _warn_unreadable(bot_id: str, path: Path) -> None:
    """Report a token hash this process cannot open — once per bot.

    This is the line the 2026-09-04 phone test did not have. An unreadable
    hash is a POD defect (a root-owned store the ``evolve`` daemon cannot
    read), not a bad credential, and it presents as an ordinary 401 to
    everyone holding a perfectly good link.
    """
    with _UNREADABLE_LOCK:
        if bot_id in _UNREADABLE_WARNED:
            return
        _UNREADABLE_WARNED.add(bot_id)
    log.warning(
        "board token hash for %s is present but unreadable by this process "
        "(%s) — every board request will 401 with a valid link until the "
        "store is owned by the admin daemon's user. Run `%s` on the pod host.",
        bot_id, path, REPAIR_COMMAND,
    )


def mint_token(shared_dir: Path, bot_id: str) -> str:
    """Mint (or rotate) the board token. Returns the token — the only time
    it is ever visible; the store keeps just the hash, mode 0600."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    d = board_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    p = _token_path(shared_dir, bot_id)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(digest + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # The mint CLI runs under sudo, so without this the hash is root-owned at
    # 0600 and the daemon that has to verify against it gets EACCES on every
    # request — the 2026-09-04 phone test. 0600 is unchanged; only the owner
    # moves, to the account that reads it.
    _adopt_store(shared_dir, bot_id, p)
    return token


def revoke_token(shared_dir: Path, bot_id: str) -> bool:
    """Delete the bot's token hash. Returns True if one was there.

    This is the hard kill for a leaked link: with no hash file the board
    refuses every request (fail closed), including one presenting a cookie
    minted from the old token. Minting again issues a fresh, unrelated
    credential — there is no way to "un-revoke".
    """
    p = _token_path(shared_dir, bot_id)
    try:
        p.unlink()
        had = True
    except FileNotFoundError:
        had = False
    # Revoking under sudo must not leave a root-owned store behind either:
    # the next mint would land in it, and a mint that leaves the daemon
    # locked out is the whole failure this store guards against.
    _adopt_store(shared_dir, bot_id)
    return had


def _read_token_digest(shared_dir: Path, bot_id: str) -> tuple[str | None, bool]:
    """``(stored digest or None, readable)`` for this bot's token hash.

    ``readable`` is False for exactly one case: the hash file EXISTS and this
    process cannot open it. That is a pod-side ownership defect, and it is
    the case the caller must not treat as a bad credential — see
    :func:`token_store_readable`. Everything else (no file at all, a bot id
    that cannot name one, an unreadable-for-other-reasons OSError) reports
    ``True``: those are honest, fail-closed "no token here" answers.
    """
    try:
        path = _token_path(shared_dir, bot_id)
    except ValueError:  # a bot id that could never have a hash file
        return None, True
    try:
        return path.read_text(encoding="utf-8").strip(), True
    except FileNotFoundError:
        return None, True
    except PermissionError:
        _warn_unreadable(bot_id, path)
        return None, False
    except (ValueError, OSError):
        return None, True


def token_store_readable(shared_dir: Path, bot_id: str) -> bool:
    """False only when this bot's token hash exists but cannot be read.

    The caller (``routes_board._auth``) uses this to decide whether a failed
    authentication is the CLIENT's fault. An unminted or revoked board is the
    client's problem (fail closed, charge the failed-auth limiter); a hash
    the daemon cannot open is the POD's, and charging the limiter for it is
    how one broken store turned into a window in which the correct token was
    also refused. Emits the one-per-bot warning as a side effect of the read.
    """
    return _read_token_digest(shared_dir, bot_id)[1]


def verify_token(shared_dir: Path, bot_id: str, presented: str | None) -> bool:
    """Constant-time check. Missing hash file, empty token, bad bot id — all
    False; nothing here raises on hostile input.

    An UNREADABLE hash file is also False (fail closed), but it is not
    silent: it warns once per bot per process, and
    :func:`token_store_readable` lets the route tell the two apart.
    """
    if not presented:
        return False
    stored, _readable = _read_token_digest(shared_dir, bot_id)
    if stored is None:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored, digest)


# ── D-MB6 importer ─────────────────────────────────────────────────────────

#: Section-heading → skip. Completed work is history, not a card.
_DONE_HEADING = re.compile(r"^##.*COMPLETED", re.IGNORECASE)

#: Crude cluster guesser for imported rows. The user re-clusters by triage;
#: a wrong guess costs one drag, a missing card costs a forgotten task —
#: so the importer prefers guessing to dropping.
_CLUSTER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("health", ("dr.", "doctor", "dental", "crown", "cardio", "scan", "medical",
                "dexa", "rx", "appointment", "derm", "eye exam", "sleep")),
    ("travel", ("trip", "flight", "hotel", "vegas", "travel")),
    ("fitness", ("gym", "training", "workout", "tennis", "run ", "whoop")),
    ("work", ("integration", "backup", "cron", "api", "database", "system")),
)


def _guess_cluster(text: str, section: str) -> str:
    hay = text.lower()
    for cluster, words in _CLUSTER_HINTS:
        if any(w in hay for w in words):
            return cluster
    if "technical" in section.lower():
        return "work"
    return "admin"


def import_tasks_md(text: str) -> list[dict[str, str]]:
    """Parse ``| # | Task | Context | ... |`` markdown tables into
    ``{title, note, cluster}`` rows, skipping completed sections, header
    rows, separator rows, and struck-through (``~~``) items."""
    rows: list[dict[str, str]] = []
    section = ""
    skipping = False
    for line in text.splitlines():
        if line.startswith("##"):
            section = line
            skipping = bool(_DONE_HEADING.match(line))
            continue
        if skipping or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        title = cells[1]
        if (not title or set(title) <= {"-", " ", ":"}
                or title.lower() in ("task", "tasks")
                or title.startswith("~~") or title.startswith("✅")):
            continue
        note = cells[2] if len(cells) > 2 else ""
        rows.append({
            "title": title,
            "note": note,
            "cluster": _guess_cluster(f"{title} {note}", section),
        })
    return rows


def import_tasks_into_board(shared_dir: Path, bot_id: str, tasks_text: str) -> int:
    """Idempotent-by-title seed of the board from a markdown task list.
    Returns how many cards were added."""
    board = load_board(shared_dir, bot_id)
    have = {c["title"] for c in board["cards"]}
    added = 0
    for row in import_tasks_md(tasks_text):
        if row["title"] in have:
            continue
        add_card(board, title=row["title"], note=row["note"],
                 cluster=row["cluster"], lane="inbox", source="import")
        have.add(row["title"])
        added += 1
    if added:
        save_board(shared_dir, bot_id, board)
        append_event(shared_dir, bot_id,
                     {"event": "imported", "cards_added": added, "actor": "operator"})
    return added


def import_legacy_board_events(shared_dir: Path, bot_id: str, events_text: str) -> int:
    """D-MB6: fold a hand-planted ``state/board-events.jsonl`` prototype's
    settle decisions onto cards :func:`import_tasks_into_board` already
    imported. Returns how many cards were moved.

    The WoZ-1 prototype's event log predates this store and carries no fixed
    schema, so this reads one JSON object per line, best-effort — anything
    it cannot parse is skipped, never raised:

        {"title": "...", "lane": "done"}      # or "dropped"
        {"title": "...", "done": true}        # folds to lane "done"
        {"title": "...", "dropped": true}     # folds to lane "dropped"

    Matches by exact card title. A title with no matching card, or a card
    already settled (or already in the target lane), is skipped — this is a
    settle-FORWARD only: it never resurrects a card the user never had and
    never re-applies once a title has already landed in ``done``/``dropped``,
    so re-running the same migrate command twice is a no-op the second time.
    """
    moved = 0
    for line in events_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        lane = row.get("lane")
        if lane not in SETTLED_LANES:
            if row.get("done"):
                lane = "done"
            elif row.get("dropped"):
                lane = "dropped"
            else:
                continue
        board = load_board(shared_dir, bot_id)
        card = next((c for c in board["cards"] if c.get("title") == title), None)
        if card is None or card.get("lane") == lane or card.get("lane") in SETTLED_LANES:
            continue
        try:
            move_card(shared_dir, bot_id, card["id"], lane, actor="operator")
        except (KeyError, ValueError):
            continue
        moved += 1
    return moved


def default_legacy_cron_label(bot_id: str) -> str:
    """The WoZ-1 hand-planted cron's label, absent an operator override —
    same ``ai.evolve.<bot>.<name>`` convention the gallery scheduled-action
    plists use (see ``gallery/morning-board``'s manifest)."""
    return f"ai.evolve.{bot_id}.morning-board"


def disable_legacy_cron(label: str) -> tuple[bool, str]:
    """Best-effort ``launchctl bootout`` of the hand-planted WoZ-1 cron
    (D-MB6). Never raises — a missing or already-gone label must not fail
    the migration around it; the caller logs the returned message either way
    ('says so in the install log')."""
    import subprocess
    try:
        result = subprocess.run(
            ["sudo", "/bin/launchctl", "bootout", f"system/{label}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run launchctl bootout for {label}: {exc}"
    if result.returncode == 0:
        return True, f"disabled hand-planted cron {label}"
    stderr = (result.stderr or result.stdout or "").strip()
    if "Could not find" in stderr or "No such process" in stderr:
        return False, (f"hand-planted cron {label} was not loaded "
                       "(already disabled or never installed)")
    return False, f"could not disable {label}: {stderr[:200]}"


# ── BOARD.md mirror (build item 4) ──────────────────────────────────────────
# design-pa-lists-and-board-2026-09-01.md §1: "a readable BOARD.md mirror
# exported into the workspace so user and bot can still read the board as a
# file." No writer for this existed before this chip (it was proposed,
# never built) — this is new, not an extension of hand-planted per-bot
# scaffolding, which is a different, already-retired file (D-MB6).

_MIRROR_LANE_HEADINGS = {
    "inbox": "Inbox", "today": "Today", "later": "Later",
    "done": "Done", "dropped": "Dropped",
}


def render_card_mirror_line(card: dict[str, Any]) -> str:
    """One BOARD.md line for a card — the fields the brief names by name:
    owner, next touch, outcome. A bullet, not a table: the file is for a
    bot or a person to skim or grep, not to parse as structured data (that
    reads ``board.json`` directly).
    """
    ident = card.get("human_id") or str(card.get("id") or "")[:8]
    owner = card.get("owner") or "me"
    if owner == "me":
        owner_text = "you"
    elif owner == "bot":
        owner_text = "bot"
    else:
        who = (card.get("waiting_on") or {}).get("who") or "someone"
        owner_text = f"waiting on {who}"
    meta = [owner_text]
    next_touch = card.get("next_touch")
    touch_action = card.get("touch_action")
    if next_touch and touch_action:
        label = TOUCH_ACTION_LABELS.get(touch_action, touch_action)
        meta.append(f"next {next_touch} — {label}")
    if card.get("due"):
        meta.append(f"due {card['due']}")
    line = f"- [{ident}] {card.get('title') or '(untitled)'} ({', '.join(meta)})"
    outcome = card.get("outcome")
    if outcome:
        line += f" — outcome: {outcome}"
    return line


def render_board_mirror(board: dict[str, Any], *, now: "datetime | None" = None) -> str:
    """The whole BOARD.md text — deterministic and pure, so the fixture
    test checks this string exactly, never a file on disk. One section per
    lane, one :func:`render_card_mirror_line` per visible card (a settled
    tile older than :data:`ARCHIVE_AFTER_DAYS` drops out, the same filter
    the page itself uses).
    """
    lines = ["# Board", ""]
    by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
    for card in visible_cards(board, now=now):
        by_lane.setdefault(card.get("lane") or "inbox", []).append(card)
    for lane in LANES:
        lines.append(f"## {_MIRROR_LANE_HEADINGS.get(lane, lane.title())}")
        lane_cards = by_lane.get(lane, [])
        if not lane_cards:
            lines.append("(none)")
        else:
            lines.extend(render_card_mirror_line(c) for c in lane_cards)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def board_mirror_path(shared_dir: Path, bot_id: str, *, network: Any = None) -> Path:
    """Where BOARD.md is exported: the bot's ``workspace/evolve/`` dir when
    reachable (write ACL for ``evolve``, same grant
    ``handover.write_preferences_to_bot`` relies on), else a fallback under
    this bot's board dir (always writable) so the mirror is never lost when
    a bot's home isn't reachable (undeployed, a test)."""
    try:
        from .config import bot_home
        home = bot_home(bot_id, network=network)
        target_dir = home / ".openclaw" / "workspace" / "evolve"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / "BOARD.md"
    except Exception:
        return board_dir(shared_dir, bot_id) / "BOARD.md"


def write_board_mirror(shared_dir: Path, bot_id: str, *, network: Any = None) -> Path:
    """Compose and atomically write BOARD.md for this bot. Deliberately NOT
    wired into :func:`save_board` or any writer above — every existing
    board write stays exactly as it was before this chip. A future chip
    (the touch scheduler, an operator command) calls this explicitly."""
    board = load_board(shared_dir, bot_id)
    text = render_board_mirror(board)
    path = board_mirror_path(shared_dir, bot_id, network=network)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".board-md-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if path.parent == board_dir(shared_dir, bot_id):
        _adopt_store(shared_dir, bot_id, path)
    return path


# ── module entry points ────────────────────────────────────────────────────

def _resolve_shared_dir(network: str | None) -> Path:
    from .config import CANONICAL_SHARED_DIR, load_network
    if network:
        return Path(load_network(Path(network)).get("sharedDir", CANONICAL_SHARED_DIR))
    return Path(CANONICAL_SHARED_DIR)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evolve_admin.board_store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_mint = sub.add_parser("mint", help="mint (or rotate) a bot's board token")
    p_mint.add_argument("--bot", required=True)
    p_mint.add_argument("--network", default=None)
    p_rev = sub.add_parser("revoke", help="revoke a bot's board token")
    p_rev.add_argument("--bot", required=True)
    p_rev.add_argument("--network", default=None)
    p_imp = sub.add_parser("import-tasks", help="seed the board from a markdown task list")
    p_imp.add_argument("--bot", required=True)
    p_imp.add_argument("--tasks-file", required=True)
    p_imp.add_argument("--network", default=None)
    p_mig = sub.add_parser(
        "migrate",
        help="D-MB6: one-time import of a bot's hand-planted tasks.md + "
             "board-events.jsonl, then retire its hand-planted cron")
    p_mig.add_argument("--bot", required=True)
    p_mig.add_argument("--tasks-file", required=True,
                       help="the bot's memory/tasks.md")
    p_mig.add_argument("--events-file", default=None,
                       help="the bot's state/board-events.jsonl, if present")
    p_mig.add_argument("--legacy-cron-label", default=None,
                       help="launchd label of the hand-planted morning-board "
                            "cron (default: ai.evolve.<bot>.morning-board)")
    p_mig.add_argument("--skip-cron-disable", action="store_true",
                       help="import only; leave the hand-planted cron running")
    p_mig.add_argument("--network", default=None)
    args = ap.parse_args(argv)
    shared = _resolve_shared_dir(args.network)
    if args.cmd == "mint":
        token = mint_token(shared, args.bot)
        print("Board token (shown once — the store keeps only its hash):")
        print(token)
        print(f"Page URL path: /board/{args.bot}?t={token}")
        return 0
    if args.cmd == "revoke":
        had = revoke_token(shared, args.bot)
        print(f"revoked board token for {args.bot}" if had
              else f"no board token to revoke for {args.bot}")
        return 0
    if args.cmd == "migrate":
        added = import_tasks_into_board(
            shared, args.bot, Path(args.tasks_file).read_text(encoding="utf-8"))
        print(f"migrate {args.bot}: imported {added} card(s) from {args.tasks_file}")
        if args.events_file and Path(args.events_file).exists():
            settled = import_legacy_board_events(
                shared, args.bot, Path(args.events_file).read_text(encoding="utf-8"))
            print(f"migrate {args.bot}: settled {settled} card(s) from {args.events_file}")
        elif args.events_file:
            print(f"migrate {args.bot}: no events file at {args.events_file}; skipped")
        if args.skip_cron_disable:
            print(f"migrate {args.bot}: --skip-cron-disable set; "
                  "hand-planted cron left as-is")
        else:
            label = args.legacy_cron_label or default_legacy_cron_label(args.bot)
            _disabled, message = disable_legacy_cron(label)
            print(f"migrate {args.bot}: {message}")
        return 0
    added = import_tasks_into_board(
        shared, args.bot, Path(args.tasks_file).read_text(encoding="utf-8"))
    print(f"imported {added} card(s) into {board_path(shared, args.bot)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
