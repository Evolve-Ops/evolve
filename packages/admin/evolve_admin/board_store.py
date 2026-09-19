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
from datetime import datetime, timezone
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

#: Who a card is on (D-BI7). Independent of lane; the delegation block
#: attaches to this, not to a lane.
OWNERS = ("me", "bot")

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
    }


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
    """Bring a loaded board up to the D-BI7 shape in memory. Returns whether
    anything changed.

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
    changed = False
    for card in board.get("cards", []):
        if not isinstance(card, dict):
            continue
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
) -> dict[str, Any]:
    """Append a card and return it. Full-length ids: an id-keyed store with
    short random ids silently overwrites on collision, so these are 32 hex
    chars and checked against the board anyway."""
    if lane not in LANES:
        raise ValueError(f"invalid lane: {lane!r}")
    if owner not in OWNERS:
        raise ValueError(f"invalid owner: {owner!r}")
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
    }
    if parent_id:
        card["parent_id"] = parent_id
    # The upstream id this card came from (calendar event, email message).
    # Only ever read by the stocking dedup — it is what makes "never show me
    # this again" survive a re-render of the same event (D-BI2).
    if source_id is not None and not isinstance(source_id, str):
        raise ValueError("source_id must be a string")
    source_id = (source_id or "").strip()
    if source_id:
        if len(source_id) > MAX_TITLE_CHARS:
            raise ValueError(
                f"source_id is too long (max {MAX_TITLE_CHARS} chars)")
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
) -> dict[str, Any]:
    """Move one card to a lane; append the triage event. Raises KeyError on
    an unknown card, ValueError on a bad lane or an ambiguous id prefix.

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
        card["settled_at"] = _utcnow()
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
        })
    else:
        event: dict[str, Any] = {
            "event": "triaged", "card": card_id, "title": card.get("title"),
            "from": from_lane, "to": to_lane, "actor": actor,
        }
        if to_lane == "later" and snooze_until is not None:
            event["snooze_until"] = snooze_until
        append_event(shared_dir, bot_id, event)
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
) -> dict[str, Any]:
    """Add one card and log its stocking event."""
    board = load_board(shared_dir, bot_id)
    card = add_card(board, title=title, cluster=cluster, lane=lane,
                    note=note, source=source, source_id=source_id, owner=owner,
                    enrichment=enrichment)
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "stocked", "card": card["id"], "title": card["title"],
        "cluster": cluster, "source": source, "owner": owner,
        "enriched": sorted(card.get("enrichment") or {}), "actor": actor,
    })
    return card


def assign_card(
    shared_dir: Path, bot_id: str, card_id: str, owner: str, *, actor: str,
) -> dict[str, Any]:
    """Set a card's OWNER (D-BI7) and log the hand-over.

    ``owner="bot"`` is an OFFER, not an instruction: it stamps
    ``delegation.state = "offered"`` and the bot accepts or declines from
    there (D-BI4). Assigning back to ``me`` clears the delegation block —
    a card nobody handed over has no delegation to report on.

    Idempotent: re-assigning a card to the owner it already has is a no-op
    that still returns the card, so a double-tap on the phone and a retried
    tool call both land on the same state.
    """
    if owner not in OWNERS:
        raise ValueError(f"invalid owner; one of {list(OWNERS)}")
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    from_owner = card.get("owner") or "me"
    if from_owner == owner:
        return card
    card["owner"] = owner
    if owner == "bot":
        card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
    else:
        card.pop("delegation", None)
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

    Raises ``KeyError`` on an unknown card.
    """
    board = load_board(shared_dir, bot_id)
    card = resolve_card(board, card_id)
    card_id = card["id"]
    if (card.get("owner") or "me") != "bot":
        card["owner"] = "bot"
        card["delegation"] = {"state": "offered", "updated_at": _utcnow()}
    card["instructed"] = {
        "action_id": action_id, "label": action_label,
        "est_cost": est_cost, "ts": _utcnow(),
    }
    save_board(shared_dir, bot_id, board)
    append_event(shared_dir, bot_id, {
        "event": "instruction", "card": card_id, "title": card.get("title"),
        "action_id": action_id, "action_label": action_label,
        "est_cost": est_cost, "actor": actor,
    })
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
