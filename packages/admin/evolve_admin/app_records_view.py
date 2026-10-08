"""app_records_view.py — what the records surface shows, read ONLY through the verbs (D-AD4).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.2. Brief:
``records-surface-from-the-schema``. HTTP shell and HTML:
``web/app_records_routes.py``.

THE SURFACE HAS NO PRIVATE PATH TO THE FILE. Every number and row below comes
from ``app_store.records_list`` / ``records_get`` / ``records_history`` — the
same verbs a bot calls — made as the platform's own in-daemon caller
(:data:`app_store.PLATFORM`, the directory's identity), naming the instance
explicitly. Nothing here opens SQLite, builds SQL, or calls a model; the only
queries it can express are the ones the schema declares, because the verbs
refuse the rest (``undeclared_column``).

FAIL CLOSED. :func:`load` returns an :class:`Unreadable` carrying the loader's
own reason when the app's store cannot be read (spec refused, schema not yet
migrated, no file); callers render that sentence, never a stack trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import app_spec_schema as ass
from . import app_store
from .app_store import PLATFORM, RecordsRefusal

PAGE_SIZE = 25

#: Refusals that mean "there is nothing here for you" — rendered as 404, never
#: 403, so the surface does not confirm that another instance exists.
NOT_FOUND_CODES = frozenset({
    "unknown_app", "no_store_declared", "no_store", "not_found", "unknown_table",
    "forbidden"})
#: Refusals that are the caller's input being wrong (a 400, with the message).
INPUT_CODES = frozenset({
    "undeclared_column", "bad_filter", "bad_request", "bad_value", "missing_key",
    "ledger_table"})

_FILTERABLE = frozenset({"text", "int", "real", "bool", "date", "datetime"})


@dataclass(frozen=True)
class Unreadable:
    """An app whose store this surface cannot read, and the loader's reason."""

    code: str
    reason: str


@dataclass(frozen=True)
class Loaded:
    app: app_store.DeclaredApp
    instance: str


def instances_of(shared_dir: Path, app_id: str) -> list[str]:
    """Instances of ``app_id`` that have a store file on this pod, sorted."""
    return sorted(inst for aid, inst, _ in app_store.iter_stores(shared_dir) if aid == app_id)


def pick_instance(shared_dir: Path, app_id: str, requested: str | None) -> str | list[str]:
    """The instance to show, or the list to choose from when there are several.

    A requested instance with no store file is a ``not_found`` — the same answer
    as an instance that never existed.
    """
    have = instances_of(shared_dir, app_id)
    if requested:
        if requested not in have:
            raise RecordsRefusal("not_found", f"{app_id} has no instance {requested!r}")
        return requested
    if not have:
        raise RecordsRefusal("no_store", f"{app_id} has no store on this pod")
    return have[0] if len(have) == 1 else have


def load(shared_dir: Path, app_id: str, instance: str) -> Loaded | Unreadable:
    """The app's declared store, or the reason it is not readable."""
    try:
        app = app_store.declared_app(shared_dir, app_id)
    except RecordsRefusal as exc:
        if exc.code in NOT_FOUND_CODES:
            raise
        return Unreadable(exc.code, exc.message)
    return Loaded(app, instance)


def surface_of(app: app_store.DeclaredApp, table: str) -> dict[str, Any]:
    return app.schema.surface.get(table, {})


def table_columns(app: app_store.DeclaredApp, table: str) -> dict[str, str]:
    """Declared columns for a table, in declared order (ledger: the platform's shape)."""
    tdef = app.schema.tables[table]
    if tdef.get("ledger"):
        return {"seq": "int", "thing_id": "text", "at": "datetime", "kind": "text",
                "by": "text", "amount": "real", "counterparty": "text", "note": "text"}
    return dict(tdef["columns"])


def filterable(ctype: str) -> bool:
    return ctype in _FILTERABLE or ctype.startswith(ass.REF_PREFIX)


def coerce(ctype: str, text: str) -> Any:
    """A query-string value in the column's type, or ``ValueError``."""
    if ctype == "int":
        return int(text)
    if ctype == "real":
        return float(text)
    if ctype == "bool":
        low = text.strip().lower()
        if low in ("1", "true", "yes"):
            return True
        if low in ("0", "false", "no"):
            return False
        raise ValueError(f"{text!r} is not yes/no")
    if ctype.startswith(ass.REF_PREFIX):
        return text
    return text


def list_page(shared_dir: Path, app: app_store.DeclaredApp, instance: str, table: str, *,
              flt: dict[str, Any], sort: str | None, page: int) -> dict[str, Any]:
    """One page of a table. The verb caps at ``MAX_LIST_LIMIT``, so a page past
    that cap is not reachable; ``capped`` says so rather than hiding it."""
    page = max(1, page)
    want = min(page * PAGE_SIZE, app_store.MAX_LIST_LIMIT)
    res = app_store.records_list(shared_dir, app.app_id, table, caller=PLATFORM,
                                 instance=instance, filter=flt or None, sort=sort or None,
                                 limit=want)
    rows = res["rows"][(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    total = res["total"]
    reachable = min(total, app_store.MAX_LIST_LIMIT)
    return {"rows": rows, "total": total, "page": page,
            "pages": max(1, -(-reachable // PAGE_SIZE)),
            "capped": total > app_store.MAX_LIST_LIMIT}


def detail(shared_dir: Path, app: app_store.DeclaredApp, instance: str, table: str,
           key: Any) -> dict[str, Any]:
    """One row, its derived rollups, and (for the ledger's thing table) the
    history: entries newest first, plus the row's own edit revisions."""
    got = app_store.records_get(shared_dir, app.app_id, table, key, caller=PLATFORM,
                                instance=instance)
    hist = app_store.records_history(shared_dir, app.app_id, table, key, caller=PLATFORM,
                                     instance=instance)
    has_ledger = app.schema.ledger_of() == table
    entries = sorted(hist["entries"], key=lambda e: (e["at"], e["seq"]), reverse=True) \
        if has_ledger else []
    return {"row": got["row"], "rollups": got["rollups"], "has_ledger": has_ledger,
            "entries": entries,
            "revisions": list(reversed(hist["revisions"]))}


# ── the tile ────────────────────────────────────────────────────────────────


def _merge_sums(values: list[Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in values:
        for period, amount in (v or {}).items():
            out[period] = round(out.get(period, 0.0) + float(amount), 6)
    return dict(sorted(out.items()))


def aggregate_rollup(name: str, spec: dict[str, Any], values: list[Any]) -> dict[str, Any]:
    """One declared per-thing rollup, summarised across things for the tile.

    ``{name, rollup, label, value}`` — ``value`` is display-ready text. Pure.
    """
    rtype = spec["rollup"]
    label = name.replace("_", " ")
    if rtype == "count":
        value = str(sum(int(v or 0) for v in values))
    elif rtype == "last_event_at":
        seen = [v for v in values if v]
        value = max(seen) if seen else "never"
    elif rtype == "sum":
        sums = _merge_sums(values)
        if not sums:
            value = "none yet"
        elif spec.get("period", "all") == "all":
            value = f"{sums.get('all', 0):g}"
        else:
            latest = max(sums)
            value = f"{sums[latest]:g} in {latest}"
    elif rtype == "status":
        counts: dict[str, int] = {}
        for v in values:
            counts[v if v is not None else "no entry"] = counts.get(
                v if v is not None else "no entry", 0) + 1
        value = " · ".join(f"{k} {n}" for k, n in sorted(counts.items())) or "none yet"
    else:  # holder
        value = f"{sum(1 for v in values if v)} held"
    return {"name": name, "rollup": rtype, "label": label, "value": value}


def tile(shared_dir: Path, app: app_store.DeclaredApp, instance: str) -> dict[str, Any]:
    """The app card's one summary tile for one instance: row count, last change,
    and the rollup the spec names as ``tile:`` — nothing the schema does not declare.

    Row count is ``records.list``'s ``total`` on the thing table (the first
    declared table when there is no ledger). Last change is the newest ledger
    entry's ``at``; a store with no ledger records no change times, and the tile
    says so instead of inventing one.
    """
    schema = app.schema
    thing = schema.ledger_of() or sorted(t for t, d in schema.tables.items()
                                         if not d.get("ledger"))[0]
    counted = app_store.records_list(shared_dir, app.app_id, thing, caller=PLATFORM,
                                     instance=instance, limit=1)
    out: dict[str, Any] = {"instance": instance, "table": thing, "rows": counted["total"],
                           "last_change": None, "last_change_tracked": bool(schema.ledger),
                           "rollup": None}
    if schema.ledger:
        newest = app_store.records_list(shared_dir, app.app_id, schema.ledger, caller=PLATFORM,
                                        instance=instance, sort="-at", limit=1)
        out["last_change"] = newest["rows"][0]["at"] if newest["rows"] else None
    target = schema.tile_target()
    if target and target[0] == thing:
        rname = target[1]
        spec = schema.tables[thing]["rollups"][rname]
        keycol = schema.tables[thing]["key"][0]
        things = app_store.records_list(shared_dir, app.app_id, thing, caller=PLATFORM,
                                        instance=instance, limit=app_store.MAX_LIST_LIMIT)
        values = [app_store.records_get(shared_dir, app.app_id, thing, r[keycol],
                                        caller=PLATFORM, instance=instance)["rollups"].get(rname)
                  for r in things["rows"]]
        agg = aggregate_rollup(rname, spec, values)
        agg["partial"] = counted["total"] > len(things["rows"])
        out["rollup"] = agg
    return out


__all__ = [
    "PAGE_SIZE", "NOT_FOUND_CODES", "INPUT_CODES", "Unreadable", "Loaded", "instances_of",
    "pick_instance", "load", "surface_of", "table_columns", "filterable", "coerce",
    "list_page", "detail", "aggregate_rollup", "tile",
]
