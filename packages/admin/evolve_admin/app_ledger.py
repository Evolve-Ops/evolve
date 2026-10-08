"""app_ledger.py — ledger entries, the rollups derived from them, and the D-CS7 control (D-AD3).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.3. Brief:
``app-store-and-ledger-verbs``. Store: :mod:`evolve_admin.app_store`.

THINGS + ENTRIES. Almost every collection is a set of things and a history of
events against them. The platform standardises the event::

    entry = {thing_id, kind, at, by, amount?, counterparty?, note?}

``kind`` comes from the app's declared closed set (``app_spec_schema``). The
ledger is APPEND-ONLY: ``ledger.append`` is the only way in, and neither
``records.put`` nor ``records.delete`` reaches the ledger table.

THE ENTRIES ARE THE TRUTH. Rollups — current status, current holder, last event,
a count, a sum per period — are computed here from entries on every read and
never stored. An app MAY keep a status column for speed, but
:func:`unexplained_statuses` (``evolve-admin records doctor``) fails any row
whose stored status no entry explains: the D-CS7 control, which names its
subject and reports ``unknown`` when it cannot look, rather than passing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import app_spec_schema as ass
from . import app_store
from .app_store import RecordsRefusal

MAX_NOTE = 2000
MAX_COUNTERPARTY = 200


def _refuse(code: str, message: str) -> RecordsRefusal:
    return RecordsRefusal(code, message)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def entries_for(conn: sqlite3.Connection, schema: ass.StoreSchema, thing_id: str) -> list[dict[str, Any]]:
    """Every entry for one thing, oldest first (``at``, then append order)."""
    if not schema.ledger:
        return []
    rows = conn.execute(
        f"SELECT seq, thing_id, kind, at, by, amount, counterparty, note FROM "
        f"{_q(schema.ledger)} WHERE thing_id = ? ORDER BY at ASC, seq ASC", (thing_id,))
    return [dict(r) for r in rows]


def validate_entry(schema: ass.StoreSchema, entry: Any) -> dict[str, Any]:
    """An entry in stored form, or a typed refusal. Pure — no file access."""
    if not schema.ledger:
        raise _refuse("no_ledger", "this app declares no ledger table")
    if not isinstance(entry, dict):
        raise _refuse("bad_request", "entry must be an object")
    extra = sorted(set(entry) - set(ass.LEDGER_FIELDS))
    if extra:
        raise _refuse("undeclared_column", f"an entry has no field(s) {extra}; the "
                                           f"shape is {list(ass.LEDGER_FIELDS)}")
    missing = [f for f in ass.LEDGER_REQUIRED if entry.get(f) in (None, "")]
    if missing:
        raise _refuse("missing_field", f"an entry needs {missing}")
    ledger = schema.tables[schema.ledger]
    kind = entry["kind"]
    if kind not in ledger["kinds"]:
        raise _refuse("unknown_kind", f"kind {kind!r} is not in this app's closed set "
                                      f"{ledger['kinds']}")
    tbl = schema.ledger
    out: dict[str, Any] = {
        "thing_id": app_store.encode_value(tbl, "thing_id", "text", entry["thing_id"]),
        "kind": kind,
        "at": app_store.encode_value(tbl, "at", "datetime", entry["at"])
        if "T" in str(entry["at"]) else app_store.encode_value(tbl, "at", "date", entry["at"]),
        "by": app_store.encode_value(tbl, "by", "text", entry["by"])[:200],
        "amount": app_store.encode_value(tbl, "amount", "real", entry.get("amount")),
        "counterparty": app_store.encode_value(tbl, "counterparty", "text", entry.get("counterparty")),
        "note": app_store.encode_value(tbl, "note", "text", entry.get("note")),
    }
    if out["note"] and len(out["note"]) > MAX_NOTE:
        raise _refuse("bad_value", f"note is longer than {MAX_NOTE} characters")
    if out["counterparty"] and len(out["counterparty"]) > MAX_COUNTERPARTY:
        raise _refuse("bad_value", f"counterparty is longer than {MAX_COUNTERPARTY} characters")
    return out


def append_in(conn: sqlite3.Connection, schema: ass.StoreSchema, entry: dict[str, Any]) -> dict[str, Any]:
    """Append a VALIDATED entry inside an open write transaction."""
    of, ledger = schema.ledger_of(), schema.ledger
    assert of is not None and ledger is not None  # validate_entry refused no_ledger
    key_col = schema.tables[of]["key"][0]
    if conn.execute(f"SELECT 1 FROM {_q(of)} WHERE {_q(key_col)} = ?",
                    (entry["thing_id"],)).fetchone() is None:
        raise _refuse("unknown_thing", f"no {of} row {entry['thing_id']!r} — put the thing "
                                       f"before recording what happened to it")
    cur = conn.execute(
        f"INSERT INTO {_q(ledger)} (thing_id, kind, at, by, amount, counterparty, note) "
        f"VALUES (?,?,?,?,?,?,?)",
        tuple(entry[f] for f in ("thing_id", "kind", "at", "by", "amount", "counterparty", "note")))
    return {"seq": cur.lastrowid, **entry}


def ledger_append(shared_dir: Path, app_id: str, entry: Any, *, caller: Any,
                  instance: str | None = None) -> dict[str, Any]:
    """``ledger.append`` — one entry, all-or-nothing."""
    app = app_store.declared_app(shared_dir, app_id)
    if isinstance(entry, dict) and not entry.get("by"):
        entry = {**entry, "by": "platform" if caller is app_store.PLATFORM else f"bot:{caller}"}
    clean = validate_entry(app.schema, entry)
    with app_store.open_for_verb(shared_dir, app_id, caller, "append", instance,
                                 write=True) as (conn, app, inst):
        stored = append_in(conn, app.schema, clean)
        return {"app_id": app_id, "instance": inst, "ledger": app.schema.ledger,
                "entry": stored}


# ── rollups: computed, never stored ─────────────────────────────────────────


def _period(at: str, period: str) -> str:
    return {"day": at[:10], "month": at[:7], "year": at[:4]}.get(period, "all")


def compute_rollups(rollups: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The declared derived fields for one thing, from its entries (oldest first)."""
    out: dict[str, Any] = {}
    for name, r in rollups.items():
        kinds = r.get("kinds")
        mine = [e for e in entries if kinds is None or e["kind"] in kinds]
        rtype = r["rollup"]
        if rtype == "status":
            hits = [e for e in entries if e["kind"] in r["map"]]
            out[name] = r["map"][hits[-1]["kind"]] if hits else None
        elif rtype == "holder":
            holder = None
            for e in entries:
                if e["kind"] in r["set_by"]:
                    holder = e.get("counterparty")
                elif e["kind"] in r.get("cleared_by", []):
                    holder = None
            out[name] = holder
        elif rtype == "last_event_at":
            out[name] = mine[-1]["at"] if mine else None
        elif rtype == "count":
            out[name] = len(mine)
        elif rtype == "sum":
            sums: dict[str, float] = {}
            for e in mine:
                if e.get("amount") is None:
                    continue
                p = _period(e["at"], r.get("period", "all"))
                sums[p] = round(sums.get(p, 0.0) + float(e["amount"]), 6)
            out[name] = dict(sorted(sums.items()))
    return out


# ── the D-CS7 control ───────────────────────────────────────────────────────


def unexplained_statuses(shared_dir: Path, app_id: str, instance: str) -> dict[str, Any]:
    """Rows whose stored status column no ledger entry explains.

    ``{app_id, instance, status: ok|fail|unknown, findings[], reason}``. Opens
    the store READ-ONLY, and only as the user that owns it: another uid
    (root, an operator) could leave -shm/-wal files the daemon cannot write,
    so the check reports ``unknown`` and names who can run it instead.
    """
    import os

    base = {"app_id": app_id, "instance": instance, "findings": [], "reason": ""}
    try:
        app = app_store.declared_app(shared_dir, app_id)
        path = app_store.store_path(shared_dir, app_id, instance)
        if hasattr(os, "geteuid") and path.exists() and os.geteuid() != path.stat().st_uid:
            return {**base, "status": "unknown",
                    "reason": f"{path} is owned by uid {path.stat().st_uid}; run the "
                              f"check as that user (the admin daemon's)"}
        conn = app_store._connect(path, readonly=True)  # noqa: SLF001
    except (RecordsRefusal, OSError) as exc:
        return {**base, "status": "unknown", "reason": str(exc)}
    try:
        recorded = app_store._meta(conn).get("schema_hash")  # noqa: SLF001
        if recorded != app.schema.schema_hash:
            return {**base, "status": "unknown",
                    "reason": f"schema_mismatch: file {recorded}, spec {app.schema.schema_hash}"}
        of = app.schema.ledger_of()
        findings: list[dict[str, Any]] = []
        checks = [(n, r) for n, r in (app.schema.tables.get(of or "", {}).get("rollups") or {}).items()
                  if r["rollup"] == "status" and r.get("column")]
        if of and checks:
            key_col = app.schema.tables[of]["key"][0]
            for row in conn.execute(f"SELECT * FROM {_q(of)}"):
                entries = entries_for(conn, app.schema, str(row[key_col]))
                derived = compute_rollups(dict(checks), entries)
                for name, r in checks:
                    stored = row[r["column"]]
                    if stored is not None and stored != derived[name]:
                        findings.append({
                            "table": of, "key": row[key_col], "column": r["column"],
                            "stored": stored, "derived": derived[name],
                            "why": ("no entry at all" if not entries else
                                    f"latest {sorted(r['map'])} entry says {derived[name]!r}"),
                        })
        return {**base, "status": "fail" if findings else "ok", "findings": findings}
    except sqlite3.DatabaseError as exc:
        return {**base, "status": "unknown", "reason": f"store unreadable: {exc}"}
    finally:
        conn.close()


__all__ = [
    "entries_for", "validate_entry", "append_in", "ledger_append",
    "compute_rollups", "unexplained_statuses",
]
