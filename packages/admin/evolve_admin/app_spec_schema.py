"""app_spec_schema.py — the ``store:`` and ``scope:`` blocks of an app spec (D-AD2, D-AD3, D-OP7).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.1 (the app store)
and §2.3 (ledger semantics); the per-instance file is
``internal/design-organizing-principle-2026-09-26.md`` §3 / D-OP7. Brief:
``app-store-and-ledger-verbs``.

An app declares its records in its spec::

    "scope": "bot",                       # default; "pod" = one shared instance
    "store": {
      "tables": {
        "games": {
          "columns": {"id": "text", "title": "text", "status": "text"},
          "key": ["id"],
          "rollups": {
            "current_status": {"rollup": "status", "column": "status",
                               "map": {"acquired": "owned", "sold": "gone"}},
            "current_holder": {"rollup": "holder", "set_by": ["loaned"],
                               "cleared_by": ["returned"]},
            "last_event_at":  {"rollup": "last_event_at"},
            "spend_by_month": {"rollup": "sum", "period": "month",
                               "kinds": ["acquired"]}
          }
        },
        "events": {"ledger": true, "of": "games",
                   "kinds": ["acquired", "sold", "loaned", "returned"]}
      }
    }

THIS MODULE IS PURE. It validates a block, normalizes it, hashes it, and says
whether one schema may follow another. It never opens a file — the store
(``app_store``) is the only thing that does, and it calls
:func:`check_additive` before it migrates.

THE RULES, each a typed :class:`StoreSchemaError` with an operator-legible
message:

* Column types come from a CLOSED set (:data:`COLUMN_TYPES`, plus
  ``ref:<table>`` naming another non-ledger table with a one-column key).
* At most ONE ledger table per app. Its shape is the platform's
  (:data:`LEDGER_FIELDS`), not the app's: it declares ``of`` (the thing table,
  one-column key) and ``kinds`` (the closed event vocabulary), never
  ``columns`` or ``key``.
* Rollups are declared on the thing table a ledger points at, and are
  computed from entries (``app_ledger``) — never stored.
* PRESENTATION is not schema. A table may carry ``description`` (what it is
  for — the records surface's empty state), ``screens`` (``{list|detail: "/route"}``,
  a replacement the platform links to instead of rendering) and, on the table a
  ledger is ``of``, ``tile`` (the one declared rollup the app card summarises).
  They are validated here but live in :attr:`StoreSchema.surface`, OUTSIDE the
  normalized tables, so editing a description never changes the schema hash and
  never demands a migration (design §2.2, ``records-surface-from-the-schema``).
* A schema change that removes or retypes a column, removes a table, changes a
  key, or drops a ledger kind is REFUSED: ``additive only — declare a new
  table version``. Adding tables, columns, kinds and rollups is fine.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: D-OP7: one file per app INSTANCE. ``bot`` (the default, and every app
#: today) names the instance by its bot; ``pod`` by its creation name.
SCOPE_BOT = "bot"
SCOPE_POD = "pod"
SCOPES: tuple[str, ...] = (SCOPE_BOT, SCOPE_POD)

#: The closed column-type set (brief item 1). ``ref:<table>`` is the eighth.
COLUMN_TYPES: tuple[str, ...] = (
    "text", "int", "real", "bool", "date", "datetime", "json")
REF_PREFIX = "ref:"

#: Types a key column may carry. A float or a JSON blob is not an identity.
KEY_TYPES: tuple[str, ...] = ("text", "int", "bool", "date", "datetime")

#: The ledger entry shape (design §2.3) — platform-owned, identical in every app.
LEDGER_FIELDS: tuple[str, ...] = (
    "thing_id", "kind", "at", "by", "amount", "counterparty", "note")
LEDGER_REQUIRED: tuple[str, ...] = ("thing_id", "kind", "at", "by")

#: Rollup kinds and the options each takes (design §2.3: current holder,
#: current status, spend by period; plus last-event and count).
ROLLUP_OPTIONS: dict[str, tuple[str, ...]] = {
    "status": ("map", "column"),
    "holder": ("set_by", "cleared_by"),
    "last_event_at": ("kinds",),
    "count": ("kinds",),
    "sum": ("period", "kinds"),
}
SUM_PERIODS: tuple[str, ...] = ("day", "month", "year", "all")

ADDITIVE_ONLY = "additive only — declare a new table version"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_TABLE_KEYS = frozenset({"columns", "key", "rollups", "ledger", "of", "kinds",
                         "description", "screens", "tile"})
#: Screens an app may replace, and the longest description the surface renders.
SCREENS: tuple[str, ...] = ("list", "detail")
MAX_DESCRIPTION = 400
MAX_ROUTE = 200
_ROUTE_BAD_CHARS = frozenset("\\\r\n\t <>\"'")
MAX_TABLES = 32
MAX_COLUMNS = 64
MAX_KINDS = 64


class StoreSchemaError(ValueError):
    """A ``store:``/``scope:`` block the platform refuses. ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StoreSchema:
    """A validated, normalized ``store:`` block."""

    tables: dict[str, dict[str, Any]]
    ledger: str | None
    schema_hash: str
    #: ``{table: {description?, screens?, tile?}}`` — presentation only, never hashed.
    surface: dict[str, dict[str, Any]] = field(default_factory=dict)

    def tile_target(self) -> tuple[str, str] | None:
        """``(table, rollup)`` the app card summarises, or ``None`` (count-only tile)."""
        for name in sorted(self.surface):
            rollup = self.surface[name].get("tile")
            if rollup:
                return name, rollup
        return None

    def table(self, name: str) -> dict[str, Any] | None:
        return self.tables.get(name)

    def is_ledger(self, name: str) -> bool:
        return name == self.ledger

    def ledger_of(self) -> str | None:
        return self.tables[self.ledger]["of"] if self.ledger else None

    def to_dict(self) -> dict[str, Any]:
        return {"tables": self.tables}


def _fail(code: str, message: str) -> StoreSchemaError:
    return StoreSchemaError(code, message)


def _check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise _fail("bad_name", f"{what} {value!r} must match {_NAME_RE.pattern} "
                                f"(lowercase, starts with a letter; a leading "
                                f"underscore is reserved for the platform)")
    return value


def _str_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail("bad_list", f"{what} must be a list of strings")
    return list(value)


def _validate_surface(name: str, tdef: dict[str, Any], is_ledger: bool,
                      rollups: dict[str, Any]) -> dict[str, Any]:
    """The presentation keys of one table, validated. Raises on the first bad one."""
    out: dict[str, Any] = {}
    desc = tdef.get("description")
    if desc is not None:
        if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION:
            raise _fail("bad_description", f"table {name!r} description must be a non-empty "
                                           f"string of at most {MAX_DESCRIPTION} characters")
        out["description"] = desc.strip()
    screens = tdef.get("screens")
    if screens is not None:
        if not isinstance(screens, dict) or not screens:
            raise _fail("bad_screens", f"table {name!r} screens must be an object "
                                       f"mapping {list(SCREENS)} to a route")
        clean: dict[str, str] = {}
        for screen, route in screens.items():
            if screen not in SCREENS:
                raise _fail("bad_screens", f"table {name!r} cannot replace screen "
                                           f"{screen!r}; it may replace {list(SCREENS)}")
            # A same-origin absolute path only: no scheme, no `//host`, no
            # backslash — a spec must not be able to send the operator off-site.
            if (not isinstance(route, str) or not route.startswith("/")
                    or route.startswith("//") or len(route) > MAX_ROUTE
                    or any(c in _ROUTE_BAD_CHARS for c in route)):
                raise _fail("bad_screens", f"table {name!r} screens.{screen} must be a "
                                           f"same-site path starting with a single '/'")
            clean[screen] = route
        out["screens"] = clean
    tile = tdef.get("tile")
    if tile is not None:
        if is_ledger:
            raise _fail("bad_tile", f"table {name!r} is the ledger; 'tile' names a rollup "
                                    f"on the table the ledger is of")
        if not isinstance(tile, str) or tile not in rollups:
            raise _fail("bad_tile", f"table {name!r} tile {tile!r} is not a rollup declared "
                                    f"on it (declared: {sorted(rollups)})")
        out["tile"] = tile
    return out


def validate_scope(value: Any) -> str:
    """The instance scope, defaulting to ``bot`` (D-OP7). Anything else refuses."""
    if value is None or value == "":
        return SCOPE_BOT
    if value not in SCOPES:
        raise _fail("bad_scope", f"scope {value!r} must be one of {list(SCOPES)}")
    return str(value)


def _validate_kinds(value: Any, table: str) -> list[str]:
    kinds = _str_list(value, f"table {table!r} kinds")
    if not kinds:
        raise _fail("no_kinds", f"ledger {table!r} declares no kinds — the entry "
                                f"vocabulary is a closed set the app must name")
    if len(kinds) > MAX_KINDS:
        raise _fail("too_many", f"ledger {table!r} declares more than {MAX_KINDS} kinds")
    for k in kinds:
        if not _KIND_RE.match(k):
            raise _fail("bad_name", f"ledger kind {k!r} must match {_KIND_RE.pattern}")
    if len(set(kinds)) != len(kinds):
        raise _fail("duplicate", f"ledger {table!r} lists a kind twice")
    return kinds


def _validate_rollup(table: str, name: str, spec: Any, kinds: list[str]) -> dict:
    if not isinstance(spec, dict):
        raise _fail("bad_rollup", f"rollup {table}.{name} must be an object")
    rtype = spec.get("rollup")
    if rtype not in ROLLUP_OPTIONS:
        raise _fail("bad_rollup", f"rollup {table}.{name}: 'rollup' must be one of "
                                  f"{sorted(ROLLUP_OPTIONS)}, got {rtype!r}")
    extra = set(spec) - {"rollup", *ROLLUP_OPTIONS[rtype]}
    if extra:
        raise _fail("bad_rollup", f"rollup {table}.{name} ({rtype}) does not take "
                                  f"{sorted(extra)}")
    out: dict[str, Any] = {"rollup": rtype}

    def _known(values: list[str], field: str) -> list[str]:
        unknown = [v for v in values if v not in kinds]
        if unknown:
            raise _fail("unknown_kind", f"rollup {table}.{name}.{field} names "
                                        f"undeclared kind(s) {unknown}")
        return values

    if rtype == "status":
        mapping = spec.get("map")
        if not isinstance(mapping, dict) or not mapping or not all(
                isinstance(v, str) for v in mapping.values()):
            raise _fail("bad_rollup", f"rollup {table}.{name}: 'map' must be a "
                                      f"non-empty {{kind: status}} object")
        _known(list(mapping), "map")
        out["map"] = dict(mapping)
        if "column" in spec:
            out["column"] = _check_name(spec["column"], f"rollup {table}.{name}.column")
    elif rtype == "holder":
        out["set_by"] = _known(_str_list(spec.get("set_by"), "set_by"), "set_by")
        if not out["set_by"]:
            raise _fail("bad_rollup", f"rollup {table}.{name}: 'set_by' is empty")
        out["cleared_by"] = _known(
            _str_list(spec.get("cleared_by", []), "cleared_by"), "cleared_by")
    else:
        if "kinds" in spec:
            out["kinds"] = _known(_str_list(spec["kinds"], "kinds"), "kinds")
        if rtype == "sum":
            period = spec.get("period", "all")
            if period not in SUM_PERIODS:
                raise _fail("bad_rollup", f"rollup {table}.{name}: period must be "
                                          f"one of {list(SUM_PERIODS)}")
            out["period"] = period
    return out


def validate_store(store: Any) -> StoreSchema:
    """Validate and normalize a ``store:`` block. Raises :class:`StoreSchemaError`."""
    if not isinstance(store, dict):
        raise _fail("bad_store", "store must be an object {tables: {...}}")
    extra = set(store) - {"tables"}
    if extra:
        raise _fail("bad_store", f"store takes only 'tables'; got {sorted(extra)}")
    tables_in = store.get("tables")
    if not isinstance(tables_in, dict) or not tables_in:
        raise _fail("no_tables", "store.tables must be a non-empty object")
    if len(tables_in) > MAX_TABLES:
        raise _fail("too_many", f"store declares more than {MAX_TABLES} tables")

    ledgers = [n for n, t in tables_in.items()
               if isinstance(t, dict) and t.get("ledger") is True]
    if len(ledgers) > 1:
        raise _fail("two_ledgers", f"at most one ledger table per app; "
                                   f"{sorted(ledgers)} are all ledgers")

    tables: dict[str, dict[str, Any]] = {}
    # Pass 1: plain tables (a ref or a ledger's `of` needs them all known).
    for name, tdef in sorted(tables_in.items()):
        _check_name(name, "table name")
        if name.startswith("sqlite"):
            raise _fail("bad_name", f"table name {name!r} is reserved by SQLite")
        if not isinstance(tdef, dict):
            raise _fail("bad_table", f"table {name!r} must be an object")
        extra = set(tdef) - _TABLE_KEYS
        if extra:
            raise _fail("bad_table", f"table {name!r} has unknown key(s) {sorted(extra)}")
        if "ledger" in tdef and not isinstance(tdef["ledger"], bool):
            raise _fail("bad_table", f"table {name!r}: 'ledger' must be true or false")
        if tdef.get("ledger") is True:
            continue
        if "of" in tdef or "kinds" in tdef:
            raise _fail("bad_table", f"table {name!r}: 'of'/'kinds' belong to a "
                                     f"ledger table (ledger: true)")
        cols_in = tdef.get("columns")
        if not isinstance(cols_in, dict) or not cols_in:
            raise _fail("no_columns", f"table {name!r} declares no columns")
        if len(cols_in) > MAX_COLUMNS:
            raise _fail("too_many", f"table {name!r} has more than {MAX_COLUMNS} columns")
        columns: dict[str, str] = {}
        for col, ctype in cols_in.items():
            _check_name(col, f"column {name}.{col!r}")
            if not isinstance(ctype, str) or not (
                    ctype in COLUMN_TYPES or ctype.startswith(REF_PREFIX)):
                raise _fail("bad_type", f"column {name}.{col} type {ctype!r} is not "
                                        f"one of {list(COLUMN_TYPES)} or ref:<table>")
            columns[col] = ctype
        key = _str_list(tdef.get("key"), f"table {name!r} key")
        if not key:
            raise _fail("no_key", f"table {name!r} declares no key")
        if len(set(key)) != len(key):
            raise _fail("bad_key", f"table {name!r} key lists a column twice")
        for k in key:
            if k not in columns:
                raise _fail("bad_key", f"table {name!r} key column {k!r} is not declared")
            if columns[k] not in KEY_TYPES and not columns[k].startswith(REF_PREFIX):
                raise _fail("bad_key", f"table {name!r} key column {k!r} is "
                                       f"{columns[k]} — a key must be one of {list(KEY_TYPES)}")
        tables[name] = {"columns": columns, "key": key}

    for name, t in tables.items():
        for col, ctype in t["columns"].items():
            if ctype.startswith(REF_PREFIX):
                target = ctype[len(REF_PREFIX):]
                if target not in tables:
                    raise _fail("bad_ref", f"column {name}.{col} refers to {target!r}, "
                                           f"which is not a declared non-ledger table")
                if len(tables[target]["key"]) != 1:
                    raise _fail("bad_ref", f"column {name}.{col} refers to {target!r}, "
                                           f"whose key is not a single column")

    ledger = ledgers[0] if ledgers else None
    kinds: list[str] = []
    if ledger is not None:
        ldef = tables_in[ledger]
        if "columns" in ldef or "key" in ldef or "rollups" in ldef:
            raise _fail("bad_ledger", f"ledger {ledger!r} must not declare columns/"
                                      f"key/rollups — its entry shape is the "
                                      f"platform's {list(LEDGER_FIELDS)}")
        of = ldef.get("of")
        if of not in tables:
            raise _fail("bad_ledger", f"ledger {ledger!r} 'of' must name a declared "
                                      f"non-ledger table; got {of!r}")
        if len(tables[of]["key"]) != 1:
            raise _fail("bad_ledger", f"ledger {ledger!r} is of {of!r}, whose key is "
                                      f"not a single column (thing_id is one value)")
        kinds = _validate_kinds(ldef.get("kinds"), ledger)
        tables[ledger] = {"ledger": True, "of": of, "kinds": kinds}

    for name in [n for n in tables if not tables[n].get("ledger")]:
        rollups_in = tables_in[name].get("rollups")
        if rollups_in is None:
            continue
        if not isinstance(rollups_in, dict):
            raise _fail("bad_rollup", f"table {name!r}: rollups must be an object")
        if rollups_in and (ledger is None or tables[ledger]["of"] != name):
            raise _fail("bad_rollup", f"table {name!r} declares rollups but no ledger "
                                      f"is 'of' it — rollups are computed from entries")
        rollups: dict[str, Any] = {}
        for rname, rspec in rollups_in.items():
            _check_name(rname, f"rollup {name}.{rname!r}")
            if rname in tables[name]["columns"]:
                raise _fail("bad_rollup", f"rollup {name}.{rname} shadows a column")
            r = _validate_rollup(name, rname, rspec, kinds)
            col = r.get("column")
            if col is not None and col not in tables[name]["columns"]:
                raise _fail("bad_rollup", f"rollup {name}.{rname}.column {col!r} is "
                                          f"not a declared column")
            rollups[rname] = r
        if rollups:
            tables[name]["rollups"] = rollups

    surface: dict[str, dict[str, Any]] = {}
    for name in sorted(tables):
        sdef = _validate_surface(name, tables_in[name], bool(tables[name].get("ledger")),
                                 tables[name].get("rollups") or {})
        if sdef:
            surface[name] = sdef
    if sum(1 for t in surface.values() if "tile" in t) > 1:
        raise _fail("bad_tile", "the app card shows ONE rollup; only one table may name a tile")

    normalized = {"tables": tables}
    return StoreSchema(tables=tables, ledger=ledger,
                       schema_hash=schema_hash(normalized), surface=surface)


def schema_hash(normalized: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a NORMALIZED store block."""
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check_additive(old: dict[str, Any], new: StoreSchema) -> None:
    """Refuse ``new`` unless it only ADDS to ``old`` (a normalized ``to_dict``).

    Removing or retyping a column, removing a table, changing a key, turning a
    table into (or out of) a ledger, re-pointing a ledger's ``of`` or dropping
    one of its kinds would strand rows or entries already on disk. Rollups are
    derived, so they may change freely.
    """
    for name, before in (old.get("tables") or {}).items():
        after = new.tables.get(name)
        if after is None:
            raise _fail("not_additive", f"{ADDITIVE_ONLY} (table {name!r} was removed)")
        if bool(before.get("ledger")) != bool(after.get("ledger")):
            raise _fail("not_additive", f"{ADDITIVE_ONLY} (table {name!r} changed "
                                        f"between ledger and plain)")
        if before.get("ledger"):
            if before.get("of") != after.get("of"):
                raise _fail("not_additive", f"{ADDITIVE_ONLY} (ledger {name!r} 'of' "
                                            f"changed)")
            dropped = [k for k in before.get("kinds", []) if k not in after["kinds"]]
            if dropped:
                raise _fail("not_additive", f"{ADDITIVE_ONLY} (ledger {name!r} "
                                            f"dropped kind(s) {dropped})")
            continue
        if list(before.get("key", [])) != list(after["key"]):
            raise _fail("not_additive", f"{ADDITIVE_ONLY} (table {name!r} key changed)")
        for col, ctype in before.get("columns", {}).items():
            if col not in after["columns"]:
                raise _fail("not_additive", f"{ADDITIVE_ONLY} (column {name}.{col} "
                                            f"was removed)")
            if after["columns"][col] != ctype:
                raise _fail("not_additive", f"{ADDITIVE_ONLY} (column {name}.{col} "
                                            f"retyped {ctype} -> {after['columns'][col]})")


def spec_store_problems(store: Any, scope: Any) -> list[str]:
    """The spec loader's view: problems as strings, [] = clean or absent."""
    problems: list[str] = []
    try:
        validate_scope(scope)
    except StoreSchemaError as exc:
        problems.append(exc.message)
    if store:
        try:
            validate_store(store)
        except StoreSchemaError as exc:
            problems.append(f"store: {exc.message}")
    return problems


__all__ = [
    "SCOPE_BOT", "SCOPE_POD", "SCOPES", "COLUMN_TYPES", "REF_PREFIX", "KEY_TYPES",
    "LEDGER_FIELDS", "LEDGER_REQUIRED", "ROLLUP_OPTIONS", "SUM_PERIODS",
    "SCREENS", "ADDITIVE_ONLY", "StoreSchemaError", "StoreSchema", "validate_scope",
    "validate_store", "schema_hash", "check_additive", "spec_store_problems",
]
