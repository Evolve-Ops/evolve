"""The app records store (D-AD2) — one SQLite file per app instance, one writer.

Module: ``evolve_admin/app_store.py``. Brief: ``app-store-and-ledger-verbs``
items 2, 3 and 6.

WHAT THESE PIN:
  * **The file**: created only by ``ensure_store``, at
    ``{shared}/apps/<app>/<instance>/data.sqlite``, 0600 in a 0700 dir, WAL,
    the applied schema hash recorded in it; migrations additive + idempotent.
  * **The verbs** list/get/put/delete/history do what they say, and filters
    are declared-column equality/range only.
  * **Fail closed on every refusal path**, with a typed code, and never a
    partial write (a refused put leaves no row and no revision).
  * **Single writer**: concurrent puts from many threads, one file, no lost
    write.
  * **The audience check** runs before the file is opened: a bot reaches only
    its own bot-scoped instance; ``user`` never gets delete; ``read-only``
    gets only reads; an unreadable bindings file refuses rather than widening.
  * **Install creates the file** (and a bad block refuses the install before
    any byte is written).
  * **The backup set** — the brief assumed the GitHub backup takes the shared
    dir; it does not (it backs up each bot's workspace). Pinned as a finding.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_ledger, app_store  # noqa: E402
from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin.app_store import PLATFORM, RecordsRefusal  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402
from evolve_admin.applications.app_spec_store import write_spec  # noqa: E402

APP = "collection-tracker"
BOT = "bot-a"
OTHER = "bot-b"

STORE = {
    "tables": {
        "games": {
            "columns": {"id": "text", "title": "text", "players": "int",
                        "price": "real", "boxed": "bool", "bought": "date",
                        "extras": "json", "status": "text"},
            "key": ["id"],
            "rollups": {"current_status": {"rollup": "status", "column": "status",
                                           "map": {"acquired": "owned", "sold": "gone"}}},
        },
        "wishlist": {"columns": {"game": "ref:games", "rank": "int"}, "key": ["game"]},
        "plays": {"columns": {"game": "text", "on": "date", "minutes": "int"},
                  "key": ["game", "on"]},
        "events": {"ledger": True, "of": "games", "kinds": ["acquired", "sold", "loaned"]},
    },
}


def _spec(shared: Path, store: dict = STORE, scope: str = "bot", app_id: str = APP) -> AppSpec:
    spec = AppSpec.from_dict({"app_id": app_id, "name": "Collection", "purpose": "p",
                              "store": store, "scope": scope})
    write_spec(spec, shared)
    return spec


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    _spec(s)
    app_store.ensure_store(s, APP, BOT, ass.validate_store(STORE))
    return s


def _put(shared, row, table="games", caller=BOT, **kw):
    return app_store.records_put(shared, APP, table, row, caller=caller, **kw)


def _refused(code: str, fn, *a, **kw) -> RecordsRefusal:
    with pytest.raises(RecordsRefusal) as exc:
        fn(*a, **kw)
    assert exc.value.code == code, exc.value
    return exc.value


def _count(shared, table, instance=BOT) -> int:
    with sqlite3.connect(app_store.store_path(shared, APP, instance)) as c:
        return c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


# ── the file ────────────────────────────────────────────────────────────────


def test_the_file_lives_per_instance_0600_in_0700_in_wal_with_the_hash(shared):
    path = app_store.store_path(shared, APP, BOT)
    assert path == shared / "apps" / APP / BOT / "data.sqlite"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        meta = dict(c.execute("SELECT k, v FROM _evolve_meta"))
    assert meta["schema_hash"] == ass.validate_store(STORE).schema_hash
    assert meta["app_id"] == APP and meta["instance"] == BOT
    _put(shared, {"id": "g1"})
    wal = path.with_name("data.sqlite-wal")
    if wal.exists():  # SQLite gives the WAL the database file's mode
        assert stat.S_IMODE(wal.stat().st_mode) == 0o600


def test_two_bots_with_the_same_app_never_share_a_file(shared):
    app_store.ensure_store(shared, APP, OTHER, ass.validate_store(STORE))
    _put(shared, {"id": "g1", "title": "mine"})
    assert app_store.store_path(shared, APP, BOT) != app_store.store_path(shared, APP, OTHER)
    assert _count(shared, "games", OTHER) == 0


def test_ensure_is_idempotent_and_migrates_additively(shared):
    again = app_store.ensure_store(shared, APP, BOT, ass.validate_store(STORE))
    assert again["created"] is False and again["migrated"] is False
    _put(shared, {"id": "g1", "title": "kept"})

    grown = json.loads(json.dumps(STORE))
    grown["tables"]["games"]["columns"]["condition"] = "text"
    grown["tables"]["loans"] = {"columns": {"id": "text"}, "key": ["id"]}
    grown["tables"]["events"]["kinds"].append("donated")
    _spec(shared, grown)
    r = app_store.ensure_store(shared, APP, BOT, ass.validate_store(grown))
    assert r["migrated"] is True
    got = app_store.records_get(shared, APP, "games", "g1", caller=BOT)
    assert got["row"]["title"] == "kept" and got["row"]["condition"] is None
    _put(shared, {"id": "l1"}, table="loans")


def test_a_removing_migration_is_refused_and_leaves_the_file_as_it_was(shared):
    shrunk = json.loads(json.dumps(STORE))
    shrunk["tables"]["games"]["columns"].pop("price")
    err = _refused("not_additive", app_store.ensure_store, shared, APP, BOT,
                   ass.validate_store(shrunk))
    assert "additive only — declare a new table version" in err.message
    with sqlite3.connect(app_store.store_path(shared, APP, BOT)) as c:
        meta = dict(c.execute("SELECT k, v FROM _evolve_meta"))
    assert meta["schema_hash"] == ass.validate_store(STORE).schema_hash


def test_the_store_is_never_created_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    err = _refused("store_failed", app_store.ensure_store, tmp_path, APP, BOT,
                   ass.validate_store(STORE))
    assert "root" in err.message
    assert not (tmp_path / "apps").exists()


@pytest.mark.parametrize("app_id, instance", [
    ("specs", BOT), ("packs", BOT), ("../x", BOT), (APP, "../bot"), (APP, "Bot A"), (APP, ""),
])
def test_paths_refuse_reserved_and_unsafe_segments(tmp_path, app_id, instance):
    _refused("bad_request", app_store.store_path, tmp_path, app_id, instance)


# ── the verbs ───────────────────────────────────────────────────────────────


def test_put_insert_then_replace_whole_and_get(shared):
    r = _put(shared, {"id": "g1", "title": "A", "players": 4, "price": 30, "boxed": True,
                      "bought": "2026-09-01", "extras": {"sleeves": True}})
    assert r["op"] == "insert"
    row = app_store.records_get(shared, APP, "games", "g1", caller=BOT)["row"]
    assert row == {"id": "g1", "title": "A", "players": 4, "price": 30.0, "boxed": True,
                   "bought": "2026-09-01", "extras": {"sleeves": True}, "status": None}
    assert _put(shared, {"id": "g1", "title": "B"})["op"] == "replace"
    row = app_store.records_get(shared, APP, "games", {"id": "g1"}, caller=BOT)["row"]
    assert row["title"] == "B" and row["players"] is None  # replaced WHOLE


def test_list_filters_on_declared_columns_by_equality_and_range_sorts_and_caps(shared):
    for i in range(12):
        _put(shared, {"id": f"g{i:02d}", "players": i, "boxed": i % 2 == 0})
    out = app_store.records_list(shared, APP, "games", caller=BOT,
                                 filter={"players": {"gte": 3, "lt": 7}}, sort="-players")
    assert [r["players"] for r in out["rows"]] == [6, 5, 4, 3] and out["total"] == 4
    out = app_store.records_list(shared, APP, "games", caller=BOT, filter={"boxed": True},
                                 limit=2, sort="id")
    assert out["total"] == 6 and [r["id"] for r in out["rows"]] == ["g00", "g02"]
    out = app_store.records_list(shared, APP, "games", caller=BOT, limit=10_000)
    assert out["cap"] == app_store.MAX_LIST_LIMIT and len(out["rows"]) == 12


def test_composite_keys_take_an_object(shared):
    _put(shared, {"game": "g1", "on": "2026-09-01", "minutes": 40}, table="plays")
    got = app_store.records_get(shared, APP, "plays", {"game": "g1", "on": "2026-09-01"},
                                caller=BOT)
    assert got["row"]["minutes"] == 40
    _refused("missing_key", app_store.records_get, shared, APP, "plays", "g1", caller=BOT)


def test_delete_and_history(shared):
    _put(shared, {"id": "g1", "title": "A"})
    _put(shared, {"id": "g1", "title": "B"}, by="person:the-owner")
    app_store.records_delete(shared, APP, "games", "g1", caller=BOT)
    h = app_store.records_history(shared, APP, "games", "g1", caller=BOT)
    assert [r["op"] for r in h["revisions"]] == ["insert", "replace", "delete"]
    assert h["revisions"][1]["by"] == "person:the-owner"
    assert h["revisions"][0]["by"] == f"bot:{BOT}"
    _refused("not_found", app_store.records_get, shared, APP, "games", "g1", caller=BOT)


# ── fail closed ─────────────────────────────────────────────────────────────


def test_every_refusal_path_is_typed_and_writes_nothing(shared):
    _put(shared, {"id": "g1"})
    before = (_count(shared, "games"), _count(shared, "_evolve_revisions"))
    cases = [
        ("unknown_table", app_store.records_put, (shared, APP, "nope", {"id": "x"})),
        ("undeclared_column", app_store.records_put, (shared, APP, "games", {"id": "x", "colour": "red"})),
        ("bad_value", app_store.records_put, (shared, APP, "games", {"id": "x", "players": "four"})),
        ("bad_value", app_store.records_put, (shared, APP, "games", {"id": "x", "boxed": 1})),
        ("bad_value", app_store.records_put, (shared, APP, "games", {"id": "x", "bought": "Sept 1"})),
        ("missing_key", app_store.records_put, (shared, APP, "games", {"title": "no id"})),
        ("unknown_ref", app_store.records_put, (shared, APP, "wishlist", {"game": "ghost"})),
        ("ledger_append_only", app_store.records_put, (shared, APP, "events", {"thing_id": "g1"})),
        ("bad_request", app_store.records_put, (shared, APP, "games", {})),
        ("not_found", app_store.records_delete, (shared, APP, "games", "ghost")),
        ("ledger_append_only", app_store.records_delete, (shared, APP, "events", 1)),
        ("undeclared_column", app_store.records_list, (shared, APP, "games")),
        ("unknown_app", app_store.records_get, (shared, "no-such-app", "games", "g1")),
    ]
    for code, fn, args in cases:
        kw: dict[str, Any] = {"caller": BOT}
        if fn is app_store.records_list:
            kw["filter"] = {"colour": "red"}
        _refused(code, fn, *args, **kw)
    assert (_count(shared, "games"), _count(shared, "_evolve_revisions")) == before


@pytest.mark.parametrize("flt, code", [
    ({"extras": {"gte": 1}}, "bad_filter"),
    ({"players": {"like": "%"}}, "bad_filter"),
    ({"extras": {"a": 1}}, "bad_filter"),
    ("players > 3", "bad_filter"),
    ({"players": {"gte": "three"}}, "bad_value"),
])
def test_there_is_no_query_language(shared, flt, code):
    _refused(code, app_store.records_list, shared, APP, "games", caller=BOT, filter=flt)
    _refused("undeclared_column", app_store.records_list, shared, APP, "games",
             caller=BOT, sort="rowid")


def test_a_missing_file_is_no_store_and_is_never_created_by_a_verb(tmp_path):
    shared = tmp_path / "s"
    _spec(shared)
    for fn, args in ((app_store.records_list, (shared, APP, "games")),
                     (app_store.records_put, (shared, APP, "games", {"id": "x"}))):
        _refused("no_store", fn, *args, caller=BOT)
    assert not app_store.store_path(shared, APP, BOT).exists()


def test_a_schema_hash_mismatch_refuses_until_the_migration_runs(shared):
    grown = json.loads(json.dumps(STORE))
    grown["tables"]["games"]["columns"]["condition"] = "text"
    _spec(shared, grown)  # the spec moved on; the file has not been migrated
    err = _refused("schema_mismatch", app_store.records_put, shared, APP, "games",
                   {"id": "g9"}, caller=BOT)
    assert "run the install/update" in err.message
    app_store.ensure_store(shared, APP, BOT, ass.validate_store(grown))
    _put(shared, {"id": "g9", "condition": "mint"})


def test_a_spec_without_a_store_block_is_refused(tmp_path):
    spec = AppSpec.from_dict({"app_id": "plain-app", "name": "n", "purpose": "p"})
    write_spec(spec, tmp_path)
    _refused("no_store_declared", app_store.records_list, tmp_path, "plain-app", "t", caller=BOT)


# ── single writer ───────────────────────────────────────────────────────────


def test_concurrent_puts_one_file_no_lost_write(shared):
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                _put(shared, {"id": f"w{n}-{i}", "players": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert _count(shared, "games") == 160
    assert _count(shared, "_evolve_revisions") == 160
    assert list(app_store.store_path(shared, APP, BOT).parent.glob("*.sqlite")) == [
        app_store.store_path(shared, APP, BOT)]


def test_concurrent_puts_to_one_key_serialize_to_one_row_and_every_revision(shared):
    barrier = threading.Barrier(2)

    def writer(title: str) -> None:
        barrier.wait()
        _put(shared, {"id": "same", "title": title})

    ts = [threading.Thread(target=writer, args=(t,)) for t in ("x", "y")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    h = app_store.records_history(shared, APP, "games", "same", caller=BOT)
    assert sorted(r["op"] for r in h["revisions"]) == ["insert", "replace"]
    final = app_store.records_get(shared, APP, "games", "same", caller=BOT)["row"]["title"]
    assert final == h["revisions"][-1]["row"]["title"]


# ── the audience check ──────────────────────────────────────────────────────


def test_a_bot_reaches_only_its_own_bot_scoped_instance(shared):
    app_store.ensure_store(shared, APP, OTHER, ass.validate_store(STORE))
    err = _refused("forbidden", app_store.records_list, shared, APP, "games",
                   caller=BOT, instance=OTHER)
    assert "only its own instance" in err.message
    # With no instance named, the caller's own is used — never another's.
    _put(shared, {"id": "mine"})
    assert _count(shared, "games", OTHER) == 0


def test_role_user_never_gets_delete_and_read_only_gets_only_reads(shared):
    _put(shared, {"id": "g1"})
    app_store.set_binding(shared, APP, BOT, BOT, "user")
    _put(shared, {"id": "g2"})
    app_ledger.ledger_append(shared, APP, {"thing_id": "g2", "kind": "acquired",
                                           "at": "2026-09-01"}, caller=BOT)
    err = _refused("forbidden", app_store.records_delete, shared, APP, "games", "g1", caller=BOT)
    assert "records.delete" in err.message
    app_store.set_binding(shared, APP, BOT, BOT, "read-only")
    app_store.records_list(shared, APP, "games", caller=BOT)
    app_store.records_get(shared, APP, "games", "g1", caller=BOT)
    app_store.records_history(shared, APP, "games", "g1", caller=BOT)
    _refused("forbidden", _put, shared, {"id": "g3"})
    _refused("forbidden", app_ledger.ledger_append, shared, APP,
             {"thing_id": "g1", "kind": "sold", "at": "2026-09-02"}, caller=BOT)
    assert _count(shared, "games") == 2


def test_an_unreadable_bindings_file_refuses_rather_than_widening(shared):
    (app_store.store_dir(shared, APP, BOT) / "bindings.json").write_text("{not json")
    _refused("forbidden", app_store.records_list, shared, APP, "games", caller=BOT)


def test_the_audience_check_runs_before_the_file_is_opened(shared, monkeypatch):
    opened: list[Path] = []
    real = app_store._connect  # noqa: SLF001
    monkeypatch.setattr(app_store, "_connect", lambda p, **kw: opened.append(p) or real(p, **kw))
    _refused("forbidden", app_store.records_list, shared, APP, "games",
             caller=BOT, instance=OTHER)
    assert opened == []


def test_a_pod_scoped_instance_needs_an_explicit_binding(tmp_path):
    shared = tmp_path / "s"
    _spec(shared, scope="pod", app_id="household-ledger")
    app_store.ensure_store(shared, "household-ledger", "home", ass.validate_store(STORE))
    kw = {"caller": BOT}
    _refused("bad_request", app_store.records_list, shared, "household-ledger", "games", **kw)
    _refused("forbidden", app_store.records_list, shared, "household-ledger", "games",
             instance="home", **kw)
    app_store.set_binding(shared, "household-ledger", "home", BOT, "member")
    app_store.set_binding(shared, "household-ledger", "home", OTHER, "owner")
    app_store.records_put(shared, "household-ledger", "games", {"id": "g1"},
                          instance="home", **kw)
    _refused("forbidden", app_store.records_delete, shared, "household-ledger", "games",
             "g2", instance="home", **kw)
    out = app_store.records_list(shared, "household-ledger", "games", caller=OTHER,
                                 instance="home")
    assert [r["id"] for r in out["rows"]] == ["g1"]  # ONE shared instance


def test_the_platform_caller_cannot_be_forged_by_a_string(shared):
    _refused("forbidden", app_store.records_list, shared, APP, "games",
             caller="PLATFORM", instance=BOT)
    assert app_store.records_list(shared, APP, "games", caller=PLATFORM, instance=BOT)


# ── install creates the file ────────────────────────────────────────────────


def test_install_hook_creates_the_store_and_a_bad_block_refuses_before_any_write(tmp_path):
    from evolve_admin.applications import app_install
    shared = tmp_path / "s"
    spec = _spec(shared)
    store, why = app_install._ensure_app_store(shared, spec, BOT)  # noqa: SLF001
    assert why == "" and store is not None and store["created"] is True
    assert app_store.store_path(shared, APP, BOT).exists()
    assert app_install._store_refusal(spec) == ""  # noqa: SLF001

    bad = AppSpec.from_dict({"app_id": APP, "store": {"tables": {"t": {"columns": {"id": "uuid"},
                                                                       "key": ["id"]}}}})
    assert app_install._store_refusal(bad).startswith("bad_store:")  # noqa: SLF001
    none = AppSpec.from_dict({"app_id": "plain-app"})
    assert app_install._ensure_app_store(shared, none, BOT) == (None, "")  # noqa: SLF001


def test_install_to_bot_refuses_a_bad_store_block_before_touching_the_target(tmp_path, monkeypatch):
    """The real ``install_app_to_bot`` path: a Spec whose store block does not
    validate is refused as ``bad_store`` and no store file appears."""
    from evolve_admin.applications import app_install
    shared = tmp_path / "s"
    bad = {"tables": {"t": {"columns": {"id": "text"}, "key": ["id"]},
                      "l1": {"ledger": True, "of": "t", "kinds": ["a"]},
                      "l2": {"ledger": True, "of": "t", "kinds": ["b"]}}}
    _spec(shared, bad)
    workspace = tmp_path / "ws"
    (workspace / "manifests").mkdir(parents=True)
    monkeypatch.setattr(app_install, "_resolve_bot", lambda b, n: ("u", tmp_path, workspace, ""))
    monkeypatch.setattr(app_install, "_load_pack", lambda s, a: (tmp_path, object(), ""))
    out = app_install.install_app_to_bot(APP, BOT, shared_dir=shared, network={},
                                         dry_run=False)
    assert out["ok"] is False and out["error"].startswith("bad_store:"), out
    assert "at most one ledger" in out["error"]
    assert not (shared / "apps" / APP).exists()


# ── the backup set (a finding, pinned) ──────────────────────────────────────


def test_the_github_backup_set_does_not_include_the_shared_dir_store():
    """The brief assumed "the file lives under the shared dir so the existing
    GitHub-backup job takes it with no change". Read against the job itself:
    ``packages/analyzer/backup.py`` commits the BOT's workspace repo, plus three
    named copies into it — the bot's ``openclaw.json`` (redacted), its
    ``evolve-tiers.json``, and ``{shared}/metrics/<bot>/latest.json``. Nothing
    else under the shared dir is in the set, so neither is this store.

    This pins that set, so the day the backup learns to carry app stores this
    test fails and must be rewritten as the positive proof the brief wanted.
    (The records store is also evolve-owned 0600, while the backup runs as the
    bot user — so the fix is a design decision, not a path added to a list.)
    """
    src = (_ADMIN_DIR.parent / "analyzer" / "backup.py").read_text()
    shared_reads = sorted(set(
        line.strip() for line in src.splitlines()
        if "shared_dir /" in line and "=" in line and not line.strip().startswith("#")))
    assert shared_reads == [
        'metrics_src = shared_dir / "metrics" / bot_id / "latest.json"',
        'results_dir = shared_dir / "proposals" / "apply-results"',
    ], shared_reads
    assert '"apps"' not in src and "data.sqlite" not in src
