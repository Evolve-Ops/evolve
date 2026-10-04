"""app_store.py — the platform-owned records store, one SQLite file per app INSTANCE (D-AD2).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.1, amended by
``internal/design-organizing-principle-2026-09-26.md`` D-OP7. Brief:
``app-store-and-ledger-verbs``. Schema: :mod:`evolve_admin.app_spec_schema`.
Ledger + rollups: :mod:`evolve_admin.app_ledger`.

WHERE. ``{shared_dir}/apps/<app_id>/<instance>/data.sqlite`` — ``<instance>``
is the bot id for a ``scope: bot`` app (the default, and every app today) and
the creation name for a ``scope: pod`` app. Two bots with the same app never
share a file.

ONE WRITER: THE DAEMON. The admin daemon (``evolve``) creates the file on
install (:func:`ensure_store`, the ONLY function that creates one), owns its
migrations, and is the only process that opens it. No app opens its file:
bots reach it through the six contract verbs over the daemon's unix socket
(``web/records_bot_routes.py``), where the calling bot is bound from the
socket's peer uid. Within the daemon a per-file lock serializes writers and
every write is one ``BEGIN IMMEDIATE`` transaction, so two concurrent ``put``s
land as two writes, never one lost one.

FAIL CLOSED, TYPED. Every refusal is a :class:`RecordsRefusal` with a stable
``code`` and happens BEFORE the write transaction commits: a missing file
(``no_store`` — a verb never creates one), an unknown app or table, an
undeclared column, a value of the wrong type, a schema hash that differs from
the one the spec declares (``schema_mismatch`` — the install/update that
migrates it has not run), a caller the app's bindings do not admit
(``forbidden``). There is no partial write and no fallback path.

THE FILE. WAL mode, ``0600``, in a ``0700`` directory, owned by the daemon user
(``ensure_store`` refuses to run as root, so a root CLI can never leave a file
the daemon cannot write). Deploy's ``chmod -R a+rX {shared_dir}`` would widen
it; ``secret_config_perms.check_app_record_store_modes`` re-tightens it. The
applied schema — JSON and hash — is recorded IN the file (``_evolve_meta``),
and migrations are additive and idempotent (:func:`app_spec_schema.check_additive`).

AUDIENCE. A bot-scoped instance has exactly one binding, implicitly to its
bot, as ``owner`` (D-OP4). ``bindings.json`` beside the file (daemon-written,
:func:`set_binding`) may name other bots and roles for a pod-scoped instance,
or demote the bot of a bot-scoped one. A ``user``/``member`` binding never
reaches ``records.delete``; ``read-only`` reaches only the reads.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import app_spec_schema as ass

log = logging.getLogger(__name__)

STORE_FILENAME = "data.sqlite"
BINDINGS_FILENAME = "bindings.json"
STORE_MODE = 0o600
STORE_DIR_MODE = 0o700
FORMAT_VERSION = "1"

#: Existing ``{shared_dir}/apps/`` children that are not app instances.
RESERVED_APP_IDS = frozenset({"specs", "packs"})

VERBS: tuple[str, ...] = ("list", "get", "put", "delete", "history", "append")
READ_VERBS = frozenset({"list", "get", "history"})
ROLE_VERBS: dict[str, frozenset[str]] = {
    "owner": frozenset(VERBS),
    "member": frozenset(VERBS) - {"delete"},
    "user": frozenset(VERBS) - {"delete"},
    "read-only": READ_VERBS,
}

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_TEXT = 20_000
MAX_JSON = 64_000
MAX_HISTORY = 200

_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RANGE_OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_META = "_evolve_meta"
_REVISIONS = "_evolve_revisions"

#: The caller for the platform's OWN in-daemon use (the directory module):
#: authorized as owner. An object, not a string, so no request can forge it.
PLATFORM = object()

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

#: Platform-owned apps whose spec lives in code, not in ``apps/specs/``.
#: Their ids carry a dot, which ``APP_ID_PATTERN`` never matches, so no
#: gallery or forged app can take one.
_PLATFORM_APPS: dict[str, "DeclaredApp"] = {}


class RecordsRefusal(Exception):
    """A verb or install refused. ``code`` is stable; ``status`` is the HTTP one."""

    STATUS = {"no_store": 409, "schema_mismatch": 409, "not_found": 404,
              "forbidden": 403, "unknown_app": 404, "busy": 503,
              "store_failed": 500}

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = self.STATUS.get(code, 400)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.message, "code": self.code}


@dataclass(frozen=True)
class DeclaredApp:
    app_id: str
    scope: str
    schema: ass.StoreSchema


def _refuse(code: str, message: str) -> RecordsRefusal:
    return RecordsRefusal(code, message)


def _now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _q(name: str) -> str:
    """Quote an identifier. Names are regex-validated upstream; this is belt."""
    return '"' + name.replace('"', '""') + '"'


# ── paths and declarations ───────────────────────────────────────────────────


def _segment(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.match(value) or ".." in value:
        raise _refuse("bad_request", f"{what} {value!r} is not a valid path segment")
    return value


def _app_segment(app_id: Any) -> str:
    app_id = _segment(app_id, "app_id")
    if app_id in RESERVED_APP_IDS:
        raise _refuse("bad_request", f"app_id {app_id!r} is reserved under apps/")
    return app_id


def store_dir(shared_dir: Path, app_id: str, instance: str) -> Path:
    return Path(shared_dir) / "apps" / _app_segment(app_id) / _segment(instance, "instance")


def store_path(shared_dir: Path, app_id: str, instance: str) -> Path:
    """``{shared_dir}/apps/<app_id>/<instance>/data.sqlite``."""
    return store_dir(shared_dir, app_id, instance) / STORE_FILENAME


def register_platform_app(app_id: str, store: dict[str, Any], scope: str = ass.SCOPE_BOT) -> DeclaredApp:
    """Declare a platform-owned app (spec in code). Idempotent for one schema."""
    if "." not in app_id:
        raise ValueError("platform app ids carry a dot so no gallery app can collide")
    app = DeclaredApp(app_id, ass.validate_scope(scope), ass.validate_store(store))
    _PLATFORM_APPS[app_id] = app
    return app


def is_platform_app(app_id: Any) -> bool:
    """True for an app id the platform owns (D-AD8): registered in code, or
    dotted — the dot is the platform namespace (``register_platform_app``), so a
    store module not yet imported in this process still reads as platform-owned."""
    return isinstance(app_id, str) and (app_id in _PLATFORM_APPS or "." in app_id)


def declared_app(shared_dir: Path, app_id: str) -> DeclaredApp:
    """The app's declared store, from the platform registry or its spec.

    Refuses ``unknown_app`` (no spec) and ``no_store_declared`` (a spec with no
    ``store:`` block); a spec whose block does not validate refuses with the
    schema's own code — a malformed declaration is never half-honored.
    """
    _app_segment(app_id)
    if app_id in _PLATFORM_APPS:
        return _PLATFORM_APPS[app_id]
    from .applications.app_spec_store import load_spec
    spec = load_spec(Path(shared_dir), app_id)
    if spec is None:
        raise _refuse("unknown_app", f"no app {app_id!r} is defined on this pod")
    if not spec.store:
        raise _refuse("no_store_declared", f"app {app_id!r} declares no store:")
    try:
        return DeclaredApp(app_id, ass.validate_scope(spec.scope), ass.validate_store(spec.store))
    except ass.StoreSchemaError as exc:
        raise _refuse(exc.code, f"app {app_id!r}: {exc.message}") from exc


# ── bindings (the audience check) ───────────────────────────────────────────


def _bindings_path(shared_dir: Path, app_id: str, instance: str) -> Path:
    return store_dir(shared_dir, app_id, instance) / BINDINGS_FILENAME


def _read_bindings(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # Unreadable is not "no bindings": refuse rather than fall back to the
        # implicit owner, which would widen access on a read error.
        raise _refuse("forbidden", f"bindings for this instance are unreadable ({exc})") from exc
    bots = data.get("bots") if isinstance(data, dict) else None
    if not isinstance(bots, dict):
        raise _refuse("forbidden", "bindings file is malformed")
    return {str(b): str(r) for b, r in bots.items()}


def set_binding(shared_dir: Path, app_id: str, instance: str, bot_id: str, role: str) -> None:
    """Bind ``bot_id`` to an instance with ``role`` (daemon-side; D-OP4)."""
    if role not in ROLE_VERBS:
        raise _refuse("bad_request", f"role {role!r} must be one of {sorted(ROLE_VERBS)}")
    _segment(bot_id, "bot_id")
    path = _bindings_path(shared_dir, app_id, instance)
    with _lock_for(path):
        _ensure_dir(path.parent)
        bots = _read_bindings(path) or {}
        bots[bot_id] = role
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bindings-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"bots": bots}, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def resolve_instance(app: DeclaredApp, caller: Any, instance: str | None) -> str:
    """The instance a call acts on. A bot-scoped app's instance IS the caller."""
    if app.scope == ass.SCOPE_BOT:
        if caller is PLATFORM:
            if not instance:
                raise _refuse("bad_request", "a platform call must name the instance")
            return _segment(instance, "instance")
        if instance not in (None, "", caller):
            raise _refuse("forbidden", f"{app.app_id} is bot-scoped: a bot reaches "
                                       f"only its own instance")
        return _segment(caller, "bot_id")
    if not instance:
        raise _refuse("bad_request", f"{app.app_id} is pod-scoped: name the instance")
    return _segment(instance, "instance")


def authorize(shared_dir: Path, app: DeclaredApp, instance: str, caller: Any, verb: str) -> str:
    """The caller's role on the instance, or ``forbidden``. Reads no store data."""
    if verb not in VERBS:
        raise _refuse("bad_request", f"unknown verb {verb!r}")
    if caller is PLATFORM:
        return "owner"
    bots = _read_bindings(_bindings_path(shared_dir, app.app_id, instance))
    if bots is None:
        role = "owner" if (app.scope == ass.SCOPE_BOT and caller == instance) else None
    else:
        role = bots.get(caller)
    if role not in ROLE_VERBS:
        raise _refuse("forbidden", f"bot {caller!r} has no binding to "
                                   f"{app.app_id}/{instance}")
    if verb not in ROLE_VERBS[role]:
        raise _refuse("forbidden", f"role {role!r} on {app.app_id} does not "
                                   f"include {('ledger.' if verb == 'append' else 'records.') + verb}")
    return role


# ── the file ─────────────────────────────────────────────────────────────────


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def _ensure_dir(d: Path) -> None:
    """``apps/`` (0755, as ``app_spec_store`` keeps it), then ``<app_id>/`` and
    ``<instance>/`` at 0700: nothing below ``apps/`` is anyone's but the daemon's."""
    d.parent.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    for level in (d.parent, d):
        if not level.exists():
            level.mkdir(mode=STORE_DIR_MODE, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, STORE_DIR_MODE)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open an EXISTING store. ``mode=rw`` so a missing file is never created."""
    mode = "ro" if readonly else "rw"
    try:
        conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=5.0,
                               isolation_level=None, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        if not path.exists():
            raise _refuse("no_store", f"no store at {path} — the app is not installed "
                                      f"for this instance (install creates it)") from exc
        raise _refuse("store_failed", f"cannot open {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _sql_type(ctype: str) -> str:
    return {"int": "INTEGER", "bool": "INTEGER", "real": "REAL"}.get(ctype, "TEXT")


def _create_table_sql(name: str, tdef: dict[str, Any]) -> list[str]:
    if tdef.get("ledger"):
        return [
            f"CREATE TABLE IF NOT EXISTS {_q(name)} (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "thing_id TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL, "
            "amount REAL, counterparty TEXT, note TEXT)",
            f"CREATE INDEX IF NOT EXISTS {_q('ix_' + name + '_thing')} ON {_q(name)} (thing_id, at, seq)",
        ]
    cols = ", ".join(f"{_q(c)} {_sql_type(t)}" for c, t in tdef["columns"].items())
    key = ", ".join(_q(k) for k in tdef["key"])
    return [f"CREATE TABLE IF NOT EXISTS {_q(name)} ({cols}, PRIMARY KEY ({key}))"]


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["k"]: r["v"] for r in conn.execute(f"SELECT k, v FROM {_META}")}
    except sqlite3.DatabaseError as exc:
        raise _refuse("schema_mismatch", f"store has no readable schema record ({exc})") from exc


def ensure_store(shared_dir: Path, app_id: str, instance: str,
                 schema: ass.StoreSchema) -> dict[str, Any]:
    """Create or additively migrate one instance's store. Idempotent.

    The ONLY function that creates a store file. Called by the daemon on
    install/update. Returns ``{path, created, migrated, schema_hash}``.
    Refuses ``not_additive`` (with ``app_spec_schema.ADDITIVE_ONLY``) when the
    declared schema would remove or retype anything already applied.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise _refuse("store_failed", "refusing to create an app store as root — the "
                                      "daemon user must own it (run through the admin "
                                      "daemon, or deploy will re-own it)")
    path = store_path(shared_dir, app_id, instance)
    with _lock_for(path):
        created = False
        if not path.exists():
            _ensure_dir(path.parent)
            # Create the empty file at 0600 FIRST: SQLite gives -wal/-shm the
            # database file's mode, so the WAL never lands wider than the store.
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, STORE_MODE)
            os.close(fd)
            created = True
        conn = _connect(path)
        try:
            if created:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {_META} (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_REVISIONS} (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "tbl TEXT NOT NULL, key TEXT NOT NULL, op TEXT NOT NULL, at TEXT NOT NULL, "
                "by TEXT NOT NULL, row TEXT)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_revisions_key ON {_REVISIONS} (tbl, key, seq)")
            meta = _meta(conn)
            migrated = False
            if meta.get("schema_hash") != schema.schema_hash:
                old = json.loads(meta["schema_json"]) if meta.get("schema_json") else {"tables": {}}
                try:
                    ass.check_additive(old, schema)
                except ass.StoreSchemaError as exc:
                    raise _refuse(exc.code, exc.message) from exc
                existing = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for name, tdef in schema.tables.items():
                    if name not in existing:
                        for stmt in _create_table_sql(name, tdef):
                            conn.execute(stmt)
                    elif not tdef.get("ledger"):
                        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({_q(name)})")}
                        for col, ctype in tdef["columns"].items():
                            if col not in have:
                                conn.execute(f"ALTER TABLE {_q(name)} ADD COLUMN {_q(col)} {_sql_type(ctype)}")
                for k, v in (("format", FORMAT_VERSION), ("app_id", app_id),
                             ("instance", instance),
                             ("schema_json", json.dumps(schema.to_dict(), sort_keys=True)),
                             ("schema_hash", schema.schema_hash),
                             ("migrated_at", _now())):
                    conn.execute(f"INSERT OR REPLACE INTO {_META} (k, v) VALUES (?, ?)", (k, v))
                conn.execute(f"INSERT OR IGNORE INTO {_META} (k, v) VALUES ('created_at', ?)", (_now(),))
                migrated = not created
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        with contextlib.suppress(OSError):
            os.chmod(path, STORE_MODE)
    return {"path": str(path), "created": created, "migrated": migrated,
            "schema_hash": schema.schema_hash}


def ensure_for_spec(shared_dir: Path, spec: Any, bot_id: str) -> dict[str, Any] | None:
    """Install/update hook: ensure the store an ``AppSpec`` declares, if any.

    Returns None for a spec with no ``store:``. A pod-scoped app's instance is
    created by name through the operator surface, not by a per-bot install.
    """
    if not getattr(spec, "store", None):
        return None
    try:
        schema = ass.validate_store(spec.store)
        scope = ass.validate_scope(getattr(spec, "scope", None))
    except ass.StoreSchemaError as exc:
        raise _refuse(exc.code, exc.message) from exc
    if scope != ass.SCOPE_BOT:
        return None
    return ensure_store(shared_dir, spec.app_id, bot_id, schema)


@contextlib.contextmanager
def open_for_verb(shared_dir: Path, app_id: str, caller: Any, verb: str,
                  instance: str | None = None, *, write: bool = False,
                  ) -> Iterator[tuple[sqlite3.Connection, DeclaredApp, str]]:
    """Authorize, open, check the schema hash — then yield ``(conn, app, instance)``.

    For a write the per-file lock is held and ``BEGIN IMMEDIATE`` is open; the
    body's exception rolls it back (nothing partial), a clean exit commits.
    """
    app = declared_app(shared_dir, app_id)
    inst = resolve_instance(app, caller, instance)
    authorize(shared_dir, app, inst, caller, verb)
    path = store_path(shared_dir, app_id, inst)
    if not path.exists():
        raise _refuse("no_store", f"no store for {app_id}/{inst} — the app is not "
                                  f"installed for this instance (install creates it)")
    lock = _lock_for(path) if write else contextlib.nullcontext()
    with lock:
        conn = _connect(path)
        try:
            recorded = _meta(conn).get("schema_hash")
            if recorded != app.schema.schema_hash:
                raise _refuse("schema_mismatch", f"{app_id}/{inst} was migrated to "
                                                 f"{recorded or 'nothing'}, the spec declares "
                                                 f"{app.schema.schema_hash} — run the "
                                                 f"install/update that migrates it")
            if write:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    raise _refuse("busy", f"store is busy: {exc}") from exc
            try:
                yield conn, app, inst
            except BaseException:
                if write:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                raise
            if write:
                conn.execute("COMMIT")
        finally:
            conn.close()


# ── row codec ────────────────────────────────────────────────────────────────


def _table(app: DeclaredApp, table: Any) -> dict[str, Any]:
    tdef = app.schema.table(table) if isinstance(table, str) else None
    if tdef is None:
        raise _refuse("unknown_table", f"{app.app_id} declares no table {table!r}")
    return tdef


def _columns(tdef: dict[str, Any]) -> dict[str, str]:
    if tdef.get("ledger"):
        return {"seq": "int", "thing_id": "text", "kind": "text", "at": "datetime",
                "by": "text", "amount": "real", "counterparty": "text", "note": "text"}
    return tdef["columns"]


def _is_iso(value: str, kind: str) -> bool:
    try:
        if kind == "date":
            _dt.date.fromisoformat(value)
        else:
            _dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return True
    except ValueError:
        return False


def encode_value(table: str, col: str, ctype: str, value: Any) -> Any:
    """A Python value → its stored form, or ``bad_value``."""
    if value is None:
        return None
    bad = _refuse("bad_value", f"{table}.{col} expects {ctype}, got {type(value).__name__} {value!r:.60}")
    if ctype == "text" or ctype.startswith(ass.REF_PREFIX):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise bad
        value = str(value)
        if len(value) > MAX_TEXT:
            raise _refuse("bad_value", f"{table}.{col} is longer than {MAX_TEXT} characters")
        return value
    if ctype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad
        return value
    if ctype == "real":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad
        return float(value)
    if ctype == "bool":
        if not isinstance(value, bool):
            raise bad
        return int(value)
    if ctype in ("date", "datetime"):
        if not isinstance(value, str) or not _is_iso(value, ctype):
            raise bad
        return value
    if ctype == "json":
        try:
            blob = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise bad from exc
        if len(blob) > MAX_JSON:
            raise _refuse("bad_value", f"{table}.{col} JSON is larger than {MAX_JSON} bytes")
        return blob
    raise bad


def decode_row(tdef: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, ctype in _columns(tdef).items():
        if col not in row.keys():
            continue
        v = row[col]
        if v is not None and ctype == "bool":
            v = bool(v)
        elif v is not None and ctype == "json":
            v = json.loads(v)
        out[col] = v
    return out


def key_values(table: str, tdef: dict[str, Any], key: Any) -> list[Any]:
    """A key given as a scalar (one-column key) or a ``{col: value}`` object."""
    cols = tdef["key"]
    if isinstance(key, dict):
        extra = set(key) - set(cols)
        if extra:
            raise _refuse("undeclared_column", f"{table} key has no column(s) {sorted(extra)}")
        missing = [c for c in cols if key.get(c) is None]
        if missing:
            raise _refuse("missing_key", f"{table} key needs {missing}")
        vals = [key[c] for c in cols]
    elif len(cols) == 1 and key is not None:
        vals = [key]
    else:
        raise _refuse("missing_key", f"{table} key is {cols} — pass it as an object")
    return [encode_value(table, c, tdef["columns"][c], v) for c, v in zip(cols, vals)]


def _key_blob(vals: list[Any]) -> str:
    return json.dumps(vals, separators=(",", ":"))


def _where_key(tdef: dict[str, Any]) -> str:
    return " AND ".join(f"{_q(c)} = ?" for c in tdef["key"])


# ── the verbs ────────────────────────────────────────────────────────────────


def _build_filter(table: str, tdef: dict[str, Any], flt: Any) -> tuple[str, list[Any]]:
    if flt in (None, {}):
        return "", []
    if not isinstance(flt, dict):
        raise _refuse("bad_filter", "filter must be an object {column: value | {gte,lte,gt,lt}}")
    cols = _columns(tdef)
    clauses: list[str] = []
    params: list[Any] = []
    for col, cond in flt.items():
        if col not in cols:
            raise _refuse("undeclared_column", f"{table} has no column {col!r} to filter on")
        ctype = cols[col]
        if isinstance(cond, dict):
            if not cond or set(cond) - set(_RANGE_OPS):
                raise _refuse("bad_filter", f"filter on {col}: only {sorted(_RANGE_OPS)} — "
                                            f"there is no query language")
            if ctype in ("json", "bool"):
                raise _refuse("bad_filter", f"{col} is {ctype}; range filters need an ordered type")
            for op, v in sorted(cond.items()):
                clauses.append(f"{_q(col)} {_RANGE_OPS[op]} ?")
                params.append(encode_value(table, col, ctype, v))
        else:
            if ctype == "json":
                raise _refuse("bad_filter", f"{col} is json; filter on a declared scalar column")
            if cond is None:
                clauses.append(f"{_q(col)} IS NULL")
            else:
                clauses.append(f"{_q(col)} = ?")
                params.append(encode_value(table, col, ctype, cond))
    return " WHERE " + " AND ".join(clauses), params


def records_list(shared_dir: Path, app_id: str, table: Any, *, caller: Any,
                 instance: str | None = None, filter: Any = None,  # noqa: A002
                 sort: Any = None, limit: Any = None) -> dict[str, Any]:
    """``records.list`` — declared-column equality/range filter, one sort, capped."""
    with open_for_verb(shared_dir, app_id, caller, "list", instance) as (conn, app, inst):
        tdef = _table(app, table)
        where, params = _build_filter(table, tdef, filter)
        order = ""
        if sort not in (None, ""):
            if not isinstance(sort, str):
                raise _refuse("bad_filter", "sort is one column name, '-' prefix for descending")
            col = sort.lstrip("-")
            if col not in _columns(tdef):
                raise _refuse("undeclared_column", f"{table} has no column {col!r} to sort on")
            order = f" ORDER BY {_q(col)} {'DESC' if sort.startswith('-') else 'ASC'}"
        elif tdef.get("ledger"):
            order = " ORDER BY seq ASC"
        if limit in (None, ""):
            n = DEFAULT_LIST_LIMIT
        elif isinstance(limit, int) and not isinstance(limit, bool):
            n = max(1, min(limit, MAX_LIST_LIMIT))
        else:
            raise _refuse("bad_filter", "limit must be an integer")
        total = conn.execute(f"SELECT COUNT(*) FROM {_q(table)}{where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM {_q(table)}{where}{order} LIMIT ?",
                            [*params, n]).fetchall()
        return {"app_id": app_id, "instance": inst, "table": table, "total": total,
                "cap": MAX_LIST_LIMIT, "rows": [decode_row(tdef, r) for r in rows]}


def records_get(shared_dir: Path, app_id: str, table: Any, key: Any, *, caller: Any,
                instance: str | None = None) -> dict[str, Any]:
    """``records.get`` — one row, plus the rollups its ledger declares."""
    from . import app_ledger
    with open_for_verb(shared_dir, app_id, caller, "get", instance) as (conn, app, inst):
        tdef = _table(app, table)
        if tdef.get("ledger"):
            raise _refuse("ledger_table", f"{table} is the ledger; list it, or get the thing")
        vals = key_values(table, tdef, key)
        row = conn.execute(f"SELECT * FROM {_q(table)} WHERE {_where_key(tdef)}", vals).fetchone()
        if row is None:
            raise _refuse("not_found", f"{table} has no row {vals}")
        out = decode_row(tdef, row)
        rollups = {}
        if tdef.get("rollups"):
            rollups = app_ledger.compute_rollups(
                tdef["rollups"], app_ledger.entries_for(conn, app.schema, str(vals[0])))
        return {"app_id": app_id, "instance": inst, "table": table, "row": out,
                "rollups": rollups}


def _revision(conn: sqlite3.Connection, table: str, vals: list[Any], op: str, by: str,
              row: dict[str, Any] | None) -> None:
    conn.execute(f"INSERT INTO {_REVISIONS} (tbl, key, op, at, by, row) VALUES (?,?,?,?,?,?)",
                 (table, _key_blob(vals), op, _now(), by,
                  None if row is None else json.dumps(row, sort_keys=True)))


def _by(caller: Any, by: Any) -> str:
    if isinstance(by, str) and by.strip():
        return by.strip()[:200]
    return "platform" if caller is PLATFORM else f"bot:{caller}"


def put_in(conn: sqlite3.Connection, app: DeclaredApp, table: str, row: Any, *,
           caller: Any, by: str | None = None) -> dict[str, Any]:
    """Insert-or-replace one row inside an OPEN write transaction.

    Every check happens before the ``INSERT``; a refusal raised here rolls the
    whole transaction back in :func:`open_for_verb`. The platform's own batch
    writers (the directory's one-time import) call this once per row inside
    one transaction, so an import is all-or-nothing too.
    """
    if not isinstance(row, dict) or not row:
        raise _refuse("bad_request", "row must be a non-empty object")
    tdef = _table(app, table)
    if tdef.get("ledger"):
        raise _refuse("ledger_append_only", f"{table} is the ledger — use ledger.append")
    cols = tdef["columns"]
    extra = sorted(set(row) - set(cols))
    if extra:
        raise _refuse("undeclared_column", f"{table} declares no column(s) {extra}")
    vals = key_values(table, tdef, {c: row.get(c) for c in tdef["key"]})
    encoded = {c: encode_value(table, c, t, row.get(c)) for c, t in cols.items()}
    for c, t in cols.items():
        if t.startswith(ass.REF_PREFIX) and encoded[c] is not None:
            target = t[len(ass.REF_PREFIX):]
            tk = app.schema.tables[target]["key"][0]
            hit = conn.execute(f"SELECT 1 FROM {_q(target)} WHERE {_q(tk)} = ?",
                               (encoded[c],)).fetchone()
            if hit is None:
                raise _refuse("unknown_ref", f"{table}.{c} = {encoded[c]!r} names no {target} row")
    existed = conn.execute(f"SELECT 1 FROM {_q(table)} WHERE {_where_key(tdef)}",
                           vals).fetchone() is not None
    names = list(cols)
    conn.execute(f"INSERT OR REPLACE INTO {_q(table)} ({', '.join(_q(c) for c in names)}) "
                 f"VALUES ({', '.join('?' for _ in names)})", [encoded[c] for c in names])
    stored = {c: row.get(c) for c in names}
    op = "replace" if existed else "insert"
    _revision(conn, table, vals, op, _by(caller, by), stored)
    return {"table": table, "row": stored, "op": op}


def records_put(shared_dir: Path, app_id: str, table: Any, row: Any, *, caller: Any,
                instance: str | None = None, by: str | None = None) -> dict[str, Any]:
    """``records.put`` — insert, or replace the row with the same key, whole."""
    with open_for_verb(shared_dir, app_id, caller, "put", instance, write=True) as (conn, app, inst):
        return {"app_id": app_id, "instance": inst,
                **put_in(conn, app, table, row, caller=caller, by=by)}


def records_delete(shared_dir: Path, app_id: str, table: Any, key: Any, *, caller: Any,
                   instance: str | None = None, by: str | None = None) -> dict[str, Any]:
    """``records.delete`` — owner only. A thing with ledger entries cannot go:
    the entries are the truth, so append a closing entry instead."""
    with open_for_verb(shared_dir, app_id, caller, "delete", instance, write=True) as (conn, app, inst):
        tdef = _table(app, table)
        if tdef.get("ledger"):
            raise _refuse("ledger_append_only", f"{table} is the ledger — entries are never deleted")
        vals = key_values(table, tdef, key)
        if conn.execute(f"SELECT 1 FROM {_q(table)} WHERE {_where_key(tdef)}", vals).fetchone() is None:
            raise _refuse("not_found", f"{table} has no row {vals}")
        ledger = app.schema.ledger
        if ledger and app.schema.ledger_of() == table and conn.execute(
                f"SELECT 1 FROM {_q(ledger)} WHERE thing_id = ? LIMIT 1", (str(vals[0]),)).fetchone():
            raise _refuse("has_entries", f"{table} {vals} has {ledger} entries — the ledger is "
                                         f"the truth; append a closing entry instead")
        conn.execute(f"DELETE FROM {_q(table)} WHERE {_where_key(tdef)}", vals)
        _revision(conn, table, vals, "delete", _by(caller, by), None)
        return {"app_id": app_id, "instance": inst, "table": table, "deleted": vals}


def records_history(shared_dir: Path, app_id: str, table: Any, key: Any, *, caller: Any,
                    instance: str | None = None) -> dict[str, Any]:
    """``records.history`` — the row's revisions, and its ledger entries if any."""
    from . import app_ledger
    with open_for_verb(shared_dir, app_id, caller, "history", instance) as (conn, app, inst):
        tdef = _table(app, table)
        if tdef.get("ledger"):
            raise _refuse("ledger_table", f"{table} is the ledger; ask for the thing's history")
        vals = key_values(table, tdef, key)
        revs = [{"op": r["op"], "at": r["at"], "by": r["by"],
                 "row": None if r["row"] is None else json.loads(r["row"])}
                for r in conn.execute(
                    f"SELECT op, at, by, row FROM {_REVISIONS} WHERE tbl = ? AND key = ? "
                    f"ORDER BY seq DESC LIMIT ?", (table, _key_blob(vals), MAX_HISTORY))]
        entries: list[dict[str, Any]] = []
        if app.schema.ledger_of() == table:
            entries = app_ledger.entries_for(conn, app.schema, str(vals[0]))[-MAX_HISTORY:]
        return {"app_id": app_id, "instance": inst, "table": table, "key": vals,
                "revisions": list(reversed(revs)), "entries": entries}


def platform_rows(conn: sqlite3.Connection, app: DeclaredApp, table: str) -> list[dict[str, Any]]:
    """Every row of a table, uncapped — for the platform's OWN reads only.

    ``records.list`` caps at :data:`MAX_LIST_LIMIT` because its result rides in
    a model's context; an in-daemon reader (the directory's resolver) must see
    the whole table or it would silently lose people past row 200.
    """
    tdef = _table(app, table)
    return [decode_row(tdef, r) for r in conn.execute(f"SELECT * FROM {_q(table)}")]


def read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """One ``_evolve_meta`` value (platform bookkeeping inside the store)."""
    return _meta(conn).get(key)


def write_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set one ``_evolve_meta`` value inside an open write transaction."""
    if key in ("schema_hash", "schema_json", "format", "app_id", "instance"):
        raise _refuse("bad_request", f"meta key {key!r} is owned by ensure_store")
    conn.execute(f"INSERT OR REPLACE INTO {_META} (k, v) VALUES (?, ?)", (key, value))


def iter_stores(shared_dir: Path) -> Iterator[tuple[str, str, Path]]:
    """Every ``(app_id, instance, path)`` store file under ``{shared_dir}/apps``."""
    root = Path(shared_dir) / "apps"
    if not root.is_dir():
        return
    for path in sorted(root.glob(f"*/*/{STORE_FILENAME}")):
        app_id, instance = path.parent.parent.name, path.parent.name
        if app_id in RESERVED_APP_IDS:
            continue
        yield app_id, instance, path


__all__ = [
    "STORE_FILENAME", "STORE_MODE", "STORE_DIR_MODE", "VERBS", "ROLE_VERBS",
    "PLATFORM", "RecordsRefusal", "DeclaredApp", "store_dir", "store_path",
    "register_platform_app", "is_platform_app", "declared_app", "set_binding", "resolve_instance",
    "authorize", "ensure_store", "ensure_for_spec", "open_for_verb",
    "encode_value", "decode_row", "key_values", "records_list", "records_get",
    "records_put", "records_delete", "records_history", "iter_stores",
    "put_in", "platform_rows", "read_meta", "write_meta",
    "MAX_LIST_LIMIT", "DEFAULT_LIST_LIMIT",
]
