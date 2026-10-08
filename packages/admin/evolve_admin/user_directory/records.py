"""The directory on the records layer — a ``people`` table plus a ``contacts_seen`` ledger.

Brief: ``app-store-and-ledger-verbs`` item 5 (the PA's directory is the records
layer's first consumer); design ``internal/design-app-records-layer-2026-09-26.md``
§4. The rows are the ones the ``directory-is-seeded-and-capture-is-a-reflex``
brief works with: one Person per stable identity, and one ``contacts_seen``
entry each time something is learned or asserted about them.

BEHAVIOUR UNCHANGED. :mod:`.storage` keeps its whole API (``load_directory``,
``upsert_entry``, ``mint_person_id``, ``save_directory``, ``get_entry``); only
its backing moved, from ``{shared_dir}/directory/{bot_id}.json`` to
``{shared_dir}/apps/evolve.directory/{bot_id}/data.sqlite``. A Person entry is
stored as one ``people`` row — its known fields as columns, anything else a
future field adds carried in ``extra`` so nothing a reader relied on is lost.

READ ONCE. The first time the daemon touches a bot's directory and finds no
store, it creates one and imports the old JSON in ONE transaction, recording
the source path and sha256 in the store's meta (``legacy_import``). The JSON
file is left exactly where it was — chip 2 (the records surface) renders the
new table before anything retires the old file. Only the store's owner (the
daemon user) migrates: any other process — a root CLI — keeps reading the
JSON, unchanged, until the daemon has run once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .. import app_ledger, app_store
from ..app_store import PLATFORM

log = logging.getLogger(__name__)

DIRECTORY_APP_ID = "evolve.directory"
PEOPLE = "people"
CONTACTS_SEEN = "contacts_seen"

#: Entry fields that become columns. Order is the row's column order.
ENTRY_COLUMNS: tuple[str, ...] = (
    "person_id", "emails", "contact", "identities", "names", "profile_ref", "audit")

_TEXT_COLUMNS = frozenset({"person_id", "profile_ref"})

#: How each write's provenance is recorded as a ledger kind.
KIND_BY_PROVENANCE: dict[str, str] = {
    "channel-captured": "captured",
    "bot-asserted": "asserted",
    "operator-verified": "verified",
}

STORE: dict[str, Any] = {
    "tables": {
        PEOPLE: {
            "description": "Everyone this bot knows — admitted users and the contacts it acts "
                           "toward — one row per stable identity.",
            "tile": "times_seen",
            "columns": {
                "identity_key": "text", "person_id": "text", "emails": "json",
                "contact": "json", "identities": "json", "names": "json",
                "profile_ref": "text", "audit": "json", "extra": "json",
            },
            "key": ["identity_key"],
            "rollups": {
                "last_seen_at": {"rollup": "last_event_at"},
                "times_seen": {"rollup": "count"},
            },
        },
        CONTACTS_SEEN: {
            "ledger": True, "of": PEOPLE,
            "description": "Each time something was learned or asserted about a person.",
            "kinds": ["imported", "minted", "captured", "asserted", "verified"],
        },
    },
}

APP = app_store.register_platform_app(DIRECTORY_APP_ID, STORE)

_MIGRATE_LOCKS: dict[str, threading.Lock] = {}
_MIGRATE_GUARD = threading.Lock()
#: ``shared_dir\0bot_id`` pairs this process has seen READY (store + marker).
_READY: set[str] = set()


def store_path(shared_dir: Path, bot_id: str) -> Path:
    return app_store.store_path(Path(shared_dir), DIRECTORY_APP_ID, bot_id)


def legacy_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "directory" / f"{bot_id}.json"


def row_from_entry(identity_key: str, entry: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"identity_key": identity_key}
    moved: dict[str, Any] = {}
    for col in ENTRY_COLUMNS:
        if col not in entry:
            continue
        if col in _TEXT_COLUMNS and not isinstance(entry[col], (str, type(None))):
            moved[col] = entry[col]  # a hand-edited oddity rides in extra, not lost
        else:
            row[col] = entry[col]
    extra = {k: v for k, v in entry.items() if k not in ENTRY_COLUMNS}
    extra.update(moved)
    if extra:
        row["extra"] = extra
    return row


def entry_from_row(row: dict[str, Any]) -> dict[str, Any]:
    entry = {c: row[c] for c in ENTRY_COLUMNS if row.get(c) is not None}
    entry.update(row.get("extra") or {})
    return entry


def is_store_owner(shared_dir: Path) -> bool:
    """True when this process may create the store: the daemon user, never root.

    The owner of ``{shared_dir}/apps`` (deploy chowns it to ``evolve``), or —
    before that exists — of the nearest existing ancestor of it.
    """
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        return False
    anchor = Path(shared_dir) / "apps"
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        return anchor.stat().st_uid == os.geteuid()
    except OSError:
        return False


def _lock(shared_dir: Path, bot_id: str) -> threading.Lock:
    with _MIGRATE_GUARD:
        return _MIGRATE_LOCKS.setdefault(f"{shared_dir}\0{bot_id}", threading.Lock())


def ensure_migrated(shared_dir: Path, bot_id: str, *, create: bool) -> bool:
    """True when the bot's directory reads from the records store.

    READY means the store exists AND carries its ``legacy_import`` marker —
    written in the same transaction as the import (or, when there was no JSON,
    as the record that there was none). So an import that failed half-way is
    never mistaken for an empty directory, and a JSON that appears later is
    never imported over newer rows: the old file is read exactly once.

    Only the store owner creates or imports. ``False`` means "read the JSON",
    which is what every non-owner process did before this module existed. A
    write (``create=True``) whose migration cannot complete RAISES: writing
    rows into a store the old data has not reached would let a later import
    overwrite them.
    """
    shared_dir = Path(shared_dir)
    cache_key = f"{shared_dir}\0{bot_id}"
    if cache_key in _READY:
        return True
    with _lock(shared_dir, bot_id):
        exists = store_path(shared_dir, bot_id).exists()
        legacy = legacy_path(shared_dir, bot_id)
        if exists and _imported(shared_dir, bot_id):
            _READY.add(cache_key)
            return True
        if not is_store_owner(shared_dir) or not (exists or create or legacy.exists()):
            if create:
                raise PermissionError(
                    f"the directory store for {bot_id} is created by the admin daemon "
                    f"user; this process (uid {os.geteuid()}) may not create it")
            return False
        try:
            app_store.ensure_store(shared_dir, DIRECTORY_APP_ID, bot_id, APP.schema)
            _import_legacy(shared_dir, bot_id, legacy)
        except Exception:
            if create:
                raise
            log.warning("directory migration for %s failed; reading the JSON", bot_id,
                        exc_info=True)
            return False
        _READY.add(cache_key)
        return True


def _imported(shared_dir: Path, bot_id: str) -> bool:
    with app_store.open_for_verb(shared_dir, DIRECTORY_APP_ID, PLATFORM, "list",
                                 bot_id) as (conn, _app, _inst):
        return bool(app_store.read_meta(conn, "legacy_import"))


def _import_legacy(shared_dir: Path, bot_id: str, legacy: Path) -> None:
    try:
        raw: bytes | None = legacy.read_bytes()
    except FileNotFoundError:
        raw = None
    persons: dict[str, Any] = {}
    problem = ""
    if raw is not None:
        try:
            data = json.loads(raw)
            found = data.get("persons") if isinstance(data, dict) else None
            if isinstance(found, dict):
                persons = found
            else:
                problem = "no persons object"
        except ValueError as exc:
            # The old loader treated an unparseable file as empty too — but it
            # then OVERWROTE it on the next write. Now it is left in place and
            # the marker says why nothing came across.
            problem = f"unparseable: {exc}"
    with app_store.open_for_verb(shared_dir, DIRECTORY_APP_ID, PLATFORM, "put",
                                 bot_id, write=True) as (conn, app, _inst):
        if app_store.read_meta(conn, "legacy_import"):
            return
        now = app_store._now()  # noqa: SLF001
        imported = 0
        for key, entry in sorted(persons.items()):
            if not isinstance(entry, dict):
                continue
            app_store.put_in(conn, app, PEOPLE, row_from_entry(key, entry),
                             caller=PLATFORM, by="migration")
            app_ledger.append_in(conn, app.schema, {
                "thing_id": key, "kind": "imported", "at": now, "by": "migration",
                "amount": None, "counterparty": None,
                "note": f"from directory/{bot_id}.json"})
            imported += 1
        app_store.write_meta(conn, "legacy_import", json.dumps({
            "path": str(legacy), "present": raw is not None,
            "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else "",
            "persons": imported, "problem": problem, "at": now}, sort_keys=True))


def load_persons(shared_dir: Path, bot_id: str) -> dict[str, dict[str, Any]]:
    """Every stored Person entry, keyed by identity key (store must exist)."""
    with app_store.open_for_verb(Path(shared_dir), DIRECTORY_APP_ID, PLATFORM, "list",
                                 bot_id) as (conn, app, _inst):
        return {r["identity_key"]: entry_from_row(r)
                for r in app_store.platform_rows(conn, app, PEOPLE)}


def read_entry(conn: Any, identity_key: str) -> dict[str, Any] | None:
    row = conn.execute(f'SELECT * FROM "{PEOPLE}" WHERE identity_key = ?',
                       (identity_key,)).fetchone()
    if row is None:
        return None
    return entry_from_row(app_store.decode_row(APP.schema.tables[PEOPLE], row))


def write_entries(shared_dir: Path, bot_id: str, entries: dict[str, dict[str, Any]], *,
                  by: str, kind: str, note: str) -> None:
    """Replace the given Person rows and append one ``contacts_seen`` entry each,
    all in one transaction."""
    with app_store.open_for_verb(Path(shared_dir), DIRECTORY_APP_ID, PLATFORM, "put",
                                 bot_id, write=True) as (conn, app, _inst):
        now = app_store._now()  # noqa: SLF001
        for key, entry in entries.items():
            app_store.put_in(conn, app, PEOPLE, row_from_entry(key, entry),
                             caller=PLATFORM, by=by)
            app_ledger.append_in(conn, app.schema, {
                "thing_id": key, "kind": kind, "at": now, "by": by[:200],
                "amount": None, "counterparty": None, "note": note})


__all__ = [
    "DIRECTORY_APP_ID", "PEOPLE", "CONTACTS_SEEN", "STORE", "APP",
    "KIND_BY_PROVENANCE", "store_path", "legacy_path", "row_from_entry",
    "entry_from_row", "is_store_owner", "ensure_migrated", "load_persons",
    "read_entry", "write_entries",
]
