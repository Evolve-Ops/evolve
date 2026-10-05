"""The ``store:`` / ``scope:`` spec blocks (D-AD2, D-AD3, D-OP7) — accept and refuse.

Module: ``evolve_admin/app_spec_schema.py``. Brief: ``app-store-and-ledger-verbs``
item 1. Each refusal is pinned by its stable ``code`` AND by an operator-legible
fragment of its message, so a regression that refuses for the wrong reason
fails too.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402


def _games(**over) -> dict:
    tables = {
        "games": {
            "columns": {"id": "text", "title": "text", "status": "text",
                        "bought": "date", "price": "real", "extras": "json"},
            "key": ["id"],
            "rollups": {
                "current_status": {"rollup": "status", "column": "status",
                                   "map": {"acquired": "owned", "sold": "gone",
                                           "loaned": "on_loan", "returned": "owned"}},
                "current_holder": {"rollup": "holder", "set_by": ["loaned"],
                                   "cleared_by": ["returned"]},
                "last_event_at": {"rollup": "last_event_at"},
                "spend_by_month": {"rollup": "sum", "period": "month",
                                   "kinds": ["acquired"]},
            },
        },
        "wishlist": {"columns": {"game": "ref:games", "rank": "int"}, "key": ["game"]},
        "events": {"ledger": True, "of": "games",
                   "kinds": ["acquired", "sold", "loaned", "returned"]},
    }
    tables.update(over)
    return {"tables": tables}


# ── accept ──────────────────────────────────────────────────────────────────


def test_a_full_collection_store_validates_and_normalizes():
    schema = ass.validate_store(_games())
    assert schema.ledger == "events"
    assert schema.ledger_of() == "games"
    assert schema.tables["events"] == {"ledger": True, "of": "games",
                                       "kinds": ["acquired", "sold", "loaned", "returned"]}
    assert set(schema.tables["games"]["rollups"]) == {
        "current_status", "current_holder", "last_event_at", "spend_by_month"}
    assert schema.schema_hash.startswith("sha256:")


def test_every_closed_column_type_is_accepted():
    cols = {t: t for t in ass.COLUMN_TYPES}
    cols["id"] = "text"
    ass.validate_store({"tables": {"t": {"columns": cols, "key": ["id"]}}})


def test_a_store_with_no_ledger_is_fine():
    schema = ass.validate_store({"tables": {"notes": {"columns": {"id": "int"}, "key": ["id"]}}})
    assert schema.ledger is None and schema.ledger_of() is None


def test_the_hash_is_stable_across_key_order_and_changes_with_the_schema():
    a = ass.validate_store(_games())
    reordered = {"tables": dict(reversed(list(_games()["tables"].items())))}
    assert ass.validate_store(reordered).schema_hash == a.schema_hash
    other = _games()
    other["tables"]["games"]["columns"]["notes"] = "text"
    assert ass.validate_store(other).schema_hash != a.schema_hash


def test_scope_defaults_to_bot_and_accepts_pod():
    assert ass.validate_scope(None) == "bot"
    assert ass.validate_scope("") == "bot"
    assert ass.validate_scope("pod") == "pod"


# ── refuse ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("store, code, fragment", [
    ("nope", "bad_store", "must be an object"),
    ({"tables": {}}, "no_tables", "non-empty"),
    ({"tables": {"t": {"columns": {"id": "text"}, "key": ["id"]}}, "views": {}},
     "bad_store", "only 'tables'"),
    ({"tables": {"T": {"columns": {"id": "text"}, "key": ["id"]}}}, "bad_name", "table name"),
    ({"tables": {"_t": {"columns": {"id": "text"}, "key": ["id"]}}}, "bad_name", "reserved"),
    ({"tables": {"sqlite_x": {"columns": {"id": "text"}, "key": ["id"]}}}, "bad_name", "SQLite"),
    ({"tables": {"t": {"columns": {"id": "uuid"}, "key": ["id"]}}}, "bad_type", "uuid"),
    ({"tables": {"t": {"columns": {"id": "varchar"}, "key": ["id"]}}}, "bad_type", "ref:<table>"),
    ({"tables": {"t": {"columns": {}, "key": ["id"]}}}, "no_columns", "no columns"),
    ({"tables": {"t": {"columns": {"id": "text"}, "key": []}}}, "no_key", "no key"),
    ({"tables": {"t": {"columns": {"id": "text"}, "key": ["nope"]}}}, "bad_key", "not declared"),
    ({"tables": {"t": {"columns": {"id": "json"}, "key": ["id"]}}}, "bad_key", "a key must be"),
    ({"tables": {"t": {"columns": {"id": "text"}, "key": ["id"], "index": ["id"]}}},
     "bad_table", "unknown key"),
    ({"tables": {"t": {"columns": {"id": "text", "o": "ref:ghost"}, "key": ["id"]}}},
     "bad_ref", "ghost"),
    ({"tables": {"t": {"columns": {"id": "text"}, "key": ["id"], "ledger": "yes"}}},
     "bad_table", "true or false"),
])
def test_bad_blocks_are_refused_by_code(store, code, fragment):
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == code
    assert fragment in exc.value.message


def test_two_ledgers_are_refused():
    store = _games(log2={"ledger": True, "of": "games", "kinds": ["x"]})
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == "two_ledgers"
    assert "at most one ledger" in exc.value.message


@pytest.mark.parametrize("ledger, code, fragment", [
    ({"ledger": True, "of": "games"}, "bad_list", "kinds"),
    ({"ledger": True, "of": "games", "kinds": []}, "no_kinds", "closed set"),
    ({"ledger": True, "of": "games", "kinds": ["a", "a"]}, "duplicate", "twice"),
    ({"ledger": True, "of": "games", "kinds": ["Sold!"]}, "bad_name", "Sold!"),
    ({"ledger": True, "of": "ghost", "kinds": ["a"]}, "bad_ledger", "'of'"),
    ({"ledger": True, "of": "games", "kinds": ["a"], "columns": {"x": "text"}},
     "bad_ledger", "platform's"),
])
def test_bad_ledgers_are_refused(ledger, code, fragment):
    store = _games(events=ledger)
    store["tables"]["games"].pop("rollups")
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == code and fragment in exc.value.message


def test_a_ledger_over_a_composite_key_is_refused():
    store = {"tables": {
        "t": {"columns": {"a": "text", "b": "text"}, "key": ["a", "b"]},
        "log": {"ledger": True, "of": "t", "kinds": ["x"]}}}
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == "bad_ledger" and "single column" in exc.value.message


@pytest.mark.parametrize("rollup, code, fragment", [
    ({"rollup": "median"}, "bad_rollup", "must be one of"),
    ({"rollup": "status", "map": {"stolen": "gone"}}, "unknown_kind", "stolen"),
    ({"rollup": "status", "map": {}}, "bad_rollup", "non-empty"),
    ({"rollup": "status", "map": {"sold": "gone"}, "column": "nope"}, "bad_rollup", "not a declared column"),
    ({"rollup": "holder", "set_by": []}, "bad_rollup", "'set_by' is empty"),
    ({"rollup": "sum", "period": "week"}, "bad_rollup", "period"),
    ({"rollup": "count", "period": "day"}, "bad_rollup", "does not take"),
])
def test_bad_rollups_are_refused(rollup, code, fragment):
    store = _games()
    store["tables"]["games"]["rollups"] = {"r": rollup}
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == code and fragment in exc.value.message


def test_rollups_need_a_ledger_that_points_at_the_table():
    store = _games()
    store["tables"]["wishlist"]["rollups"] = {"n": {"rollup": "count"}}
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert "computed from entries" in exc.value.message


def test_a_rollup_may_not_shadow_a_column():
    store = _games()
    store["tables"]["games"]["rollups"] = {"status": {"rollup": "last_event_at"}}
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert "shadows a column" in exc.value.message


def test_bad_scope_is_refused():
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_scope("household")
    assert exc.value.code == "bad_scope"


# ── additive only ───────────────────────────────────────────────────────────


def _old() -> dict:
    return ass.validate_store(_games()).to_dict()


def test_adding_tables_columns_kinds_and_rollups_is_additive():
    new = _games(loans={"columns": {"id": "text"}, "key": ["id"]})
    new["tables"]["games"]["columns"]["condition"] = "text"
    new["tables"]["events"]["kinds"].append("donated")
    new["tables"]["games"]["rollups"]["n"] = {"rollup": "count"}
    ass.check_additive(_old(), ass.validate_store(new))  # must not raise


@pytest.mark.parametrize("mutate, detail", [
    (lambda t: t["games"]["columns"].pop("price"), "column games.price was removed"),
    (lambda t: t["games"]["columns"].__setitem__("price", "int"), "retyped real -> int"),
    (lambda t: t.pop("wishlist"), "table 'wishlist' was removed"),
    (lambda t: t["wishlist"].__setitem__("key", ["rank"]), "key changed"),
    (lambda t: t["events"].__setitem__("kinds", ["acquired", "sold", "loaned"]),
     "dropped kind(s) ['returned']"),
])
def test_a_removing_or_retyping_change_is_refused_with_the_message(mutate, detail):
    new = _games()
    mutate(new["tables"])
    if "returned" not in new["tables"]["events"]["kinds"]:
        new["tables"]["games"]["rollups"]["current_status"]["map"].pop("returned")
        new["tables"]["games"]["rollups"]["current_holder"]["cleared_by"] = []
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.check_additive(_old(), ass.validate_store(new))
    assert exc.value.code == "not_additive"
    assert exc.value.message.startswith("additive only — declare a new table version")
    assert detail in exc.value.message


# ── the spec carries it; validate() reports it ─────────────────────────────


def test_the_spec_loader_reports_a_bad_store_block():
    spec = AppSpec.from_dict({"app_id": "collection-tracker", "store": {"tables": {}}})
    assert any(p.startswith("store: store.tables must be") for p in spec.validate())
    ok = AppSpec.from_dict({"app_id": "collection-tracker", "store": _games()})
    assert not [p for p in ok.validate() if p.startswith("store")]
    bad_scope = AppSpec.from_dict({"app_id": "collection-tracker", "scope": "house"})
    assert any("scope" in p for p in bad_scope.validate())
