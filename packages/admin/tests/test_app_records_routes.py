"""The records surface — list / detail / history / tile drawn from the schema.

Module: ``evolve_admin/web/app_records_routes.py`` (+ ``app_records_view.py``).
Brief: ``records-surface-from-the-schema`` item 7.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_ledger  # noqa: E402
from evolve_admin import app_records_view as view  # noqa: E402
from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin import app_store  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402
from evolve_admin.applications.app_spec_store import write_spec  # noqa: E402
from evolve_admin.web.app_records_routes import register_app_records_routes  # noqa: E402

APP = "collection-tracker"
BOT = "bot-a"
OTHER = "bot-b"
DESC = "Games you own, want, or have passed on."
STORE = {"tables": {
    "games": {
        "description": DESC,
        "columns": {"id": "text", "title": "text", "year": "int", "owned": "bool",
                    "extra": "json"},
        "key": ["id"],
        "tile": "spend_by_month",
        "rollups": {
            "current_status": {"rollup": "status", "map": {"acquired": "owned", "sold": "gone"}},
            "spend_by_month": {"rollup": "sum", "period": "month", "kinds": ["acquired"]},
        },
    },
    "events": {"ledger": True, "of": "games", "kinds": ["acquired", "sold", "loaned"],
               "description": "Everything that happened to a game."},
}}


def _spec(store=None, **kw) -> AppSpec:
    return AppSpec.from_dict({"app_id": APP, "name": "Collection Tracker", "purpose": "p",
                              "store": STORE if store is None else store, **kw})


@pytest.fixture()
def pod(tmp_path: Path):
    shared = tmp_path / "evolve"
    shared.mkdir()
    network = tmp_path / "network.json"
    network.write_text(json.dumps({"sharedDir": str(shared)}))
    write_spec(_spec(), shared)
    app_store.ensure_store(shared, APP, BOT, ass.validate_store(STORE))
    flask_app = Flask(__name__)
    register_app_records_routes(flask_app, network)
    return {"c": flask_app.test_client(), "shared": shared, "app": flask_app, "network": network}


def _put(pod, gid, title, year=2020, instance=BOT, **extra):
    app_store.records_put(pod["shared"], APP, "games",
                          {"id": gid, "title": title, "year": year, **extra},
                          caller=app_store.PLATFORM, instance=instance)


def _entry(pod, gid, kind, at, amount=None, who=None, note=None, instance=BOT):
    e = {"thing_id": gid, "kind": kind, "at": at, "by": "platform"}
    if amount is not None:
        e["amount"] = amount
    if who:
        e["counterparty"] = who
    if note:
        e["note"] = note
    app_ledger.ledger_append(pod["shared"], APP, e, caller=app_store.PLATFORM, instance=instance)


def _get(pod, path):
    r = pod["c"].get(path)
    return r.status_code, r.get_data(as_text=True)


# ── schema: presentation is not schema ───────────────────────────────────────


def test_presentation_keys_do_not_change_the_schema_hash():
    bare = {"tables": {t: {k: v for k, v in d.items() if k not in ("description", "tile", "screens")}
                       for t, d in STORE["tables"].items()}}
    assert (ass.validate_store(STORE).schema_hash == ass.validate_store(bare).schema_hash)
    assert ass.validate_store(STORE).tile_target() == ("games", "spend_by_month")


@pytest.mark.parametrize("patch, code", [
    ({"tile": "nope"}, "bad_tile"),
    ({"screens": {"list": "https://evil.example/x"}}, "bad_screens"),
    ({"screens": {"list": "//evil.example/x"}}, "bad_screens"),
    ({"screens": {"list": "/ok", "delete": "/x"}}, "bad_screens"),
    ({"description": ""}, "bad_description"),
])
def test_bad_presentation_is_refused_by_the_loader(patch, code):
    store = json.loads(json.dumps(STORE))
    store["tables"]["games"].update(patch)
    with pytest.raises(ass.StoreSchemaError) as exc:
        ass.validate_store(store)
    assert exc.value.code == code


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_columns_come_from_the_schema(pod):
    _put(pod, "g1", "Alpha")
    code, body = _get(pod, f"/apps/{APP}/records/games")
    assert code == 200
    for col in STORE["tables"]["games"]["columns"]:
        assert f'data-label="{col}"' in body
    assert "Alpha" in body and "resp-table" in body
    assert 'href="/apps/collection-tracker/records/games/g1"' in body


def test_list_sorts_on_a_declared_column_and_toggles_direction(pod):
    _put(pod, "g1", "Alpha", 2001)
    _put(pod, "g2", "Beta", 2005)
    _, asc = _get(pod, f"/apps/{APP}/records/games?sort=year")
    _, desc = _get(pod, f"/apps/{APP}/records/games?sort=-year")
    assert asc.index("Alpha") < asc.index("Beta")
    assert desc.index("Beta") < desc.index("Alpha")
    assert "sort=year" in desc  # the header link flips back


@pytest.mark.parametrize("qs", ["sort=colour", "sort=-colour", "f.colour=red", "fcol=colour&fval=red"])
def test_sort_and_filter_refuse_an_undeclared_column(pod, qs):
    _put(pod, "g1", "Alpha")
    code, body = _get(pod, f"/apps/{APP}/records/games?{qs}")
    assert code == 400 and "colour" in body and "Traceback" not in body


def test_filter_on_a_declared_column_narrows_and_is_typed(pod):
    _put(pod, "g1", "Alpha", 2001)
    _put(pod, "g2", "Beta", 2005)
    code, body = _get(pod, f"/apps/{APP}/records/games?fcol=year&fval=2005")
    assert code == 200 and "Beta" in body and "Alpha" not in body
    code, _ = _get(pod, f"/apps/{APP}/records/games?f.year=abc")
    assert code == 400


def test_filter_offers_only_scalar_declared_columns(pod):
    _, body = _get(pod, f"/apps/{APP}/records/games")
    sel = body.split('aria-label="Filter column">')[1].split("</select>")[0]
    assert '<option value="year">' in sel and 'value="extra"' not in sel  # json: not filterable


def test_pagination_walks_the_rows_and_says_when_capped(pod):
    for i in range(view.PAGE_SIZE + 5):
        _put(pod, f"g{i:03d}", f"T{i:03d}")
    _, p1 = _get(pod, f"/apps/{APP}/records/games?sort=id")
    _, p2 = _get(pod, f"/apps/{APP}/records/games?sort=id&page=2")
    assert "T000" in p1 and "T025" not in p1 and "page 1 of 2" in p1
    assert "T025" in p2 and "T000" not in p2 and "page 2 of 2" in p2


def test_empty_state_says_what_the_table_is_for(pod):
    _, body = _get(pod, f"/apps/{APP}/records/games")
    assert "empty-state-card" in body and DESC in body
    _, ledger = _get(pod, f"/apps/{APP}/records/events")
    assert "Everything that happened to a game." in ledger


def test_cells_are_escaped(pod):
    _put(pod, "g1", "<script>alert(1)</script>", extra={"k": "<img src=x>"})
    _, body = _get(pod, f"/apps/{APP}/records/games")
    assert "<script>alert(1)" not in body and "&lt;script&gt;" in body
    assert "<img src=x>" not in body


def test_the_ledger_lists_and_links_back_to_the_thing(pod):
    _put(pod, "g1", "Alpha")
    _entry(pod, "g1", "acquired", "2026-09-01", amount=10)
    code, body = _get(pod, f"/apps/{APP}/records/events")
    assert code == 200 and "acquired" in body
    assert 'href="/apps/collection-tracker/records/games/g1"' in body
    assert _get(pod, f"/apps/{APP}/records/events/1")[0] == 404  # entries have no detail page


# ── detail + history ─────────────────────────────────────────────────────────


def test_detail_shows_columns_and_labels_rollups_as_derived(pod):
    _put(pod, "g1", "Alpha", 1999)
    _entry(pod, "g1", "acquired", "2026-09-01", amount=12.5)
    code, body = _get(pod, f"/apps/{APP}/records/games/g1")
    assert code == 200 and "Alpha" in body and "1999" in body
    assert "current_status" in body and "owned" in body
    assert body.count("derived") >= 2  # one label per rollup row, plus the card title


def test_history_strip_is_newest_first_with_kind_by_amount_counterparty(pod):
    _put(pod, "g1", "Alpha")
    _entry(pod, "g1", "acquired", "2026-01-05", amount=20)
    _entry(pod, "g1", "loaned", "2026-03-01", who="Sam", note="for the weekend")
    _entry(pod, "g1", "sold", "2026-02-10", amount=15)
    _, body = _get(pod, f"/apps/{APP}/records/games/g1")
    order = [body.index(k) for k in ("2026-03-01", "2026-02-10", "2026-01-05")]
    assert order == sorted(order)
    assert "badge" in body and "platform" in body
    assert "with Sam" in body and "for the weekend" in body and "15" in body


def test_a_thing_with_no_entries_says_so(pod):
    _put(pod, "g1", "Alpha")
    _, body = _get(pod, f"/apps/{APP}/records/games/g1")
    assert "No entries recorded" in body


def test_a_missing_row_is_a_404(pod):
    assert _get(pod, f"/apps/{APP}/records/games/ghost")[0] == 404


# ── tile ─────────────────────────────────────────────────────────────────────


def test_tile_numbers_against_a_fixture_ledger(pod):
    for gid in ("g1", "g2", "g3"):
        _put(pod, gid, gid.upper())
    _entry(pod, "g1", "acquired", "2026-09-01", amount=20)
    _entry(pod, "g2", "acquired", "2026-09-15", amount=5.5)
    _entry(pod, "g2", "sold", "2026-10-02")
    _entry(pod, "g3", "acquired", "2026-08-30", amount=100)
    r = pod["c"].get(f"/api/apps/{APP}/records/tile")
    assert r.status_code == 200
    tile = r.get_json()["tiles"][0]
    assert tile["rows"] == 3 and tile["last_change"] == "2026-10-02"
    assert tile["rollup"]["name"] == "spend_by_month"
    assert tile["rollup"]["value"] == "25.5 in 2026-09"  # latest month, summed across things
    assert tile["href"] == f"/apps/{APP}/records"


def test_tile_without_a_named_rollup_is_count_and_last_change_only(pod):
    store = json.loads(json.dumps(STORE))
    del store["tables"]["games"]["tile"]
    write_spec(_spec(store), pod["shared"])
    _put(pod, "g1", "Alpha")
    tile = pod["c"].get(f"/api/apps/{APP}/records/tile").get_json()["tiles"][0]
    assert tile["rows"] == 1 and tile["rollup"] is None and tile["last_change"] is None


@pytest.mark.parametrize("_id, spec, values, want", [
    ("count", {"rollup": "count"}, [1, 2, None], "3"),
    ("last", {"rollup": "last_event_at"}, ["2026-01-01", None, "2026-05-01"], "2026-05-01"),
    ("status", {"rollup": "status", "map": {}}, ["owned", "owned", "gone", None], "gone 1 · no entry 1 · owned 2"),
    ("holder", {"rollup": "holder", "set_by": ["loaned"]}, ["Sam", None, "Kim"], "2 held"),
    ("sum_all", {"rollup": "sum", "period": "all"}, [{"all": 3.0}, {"all": 4.5}], "7.5"),
    ("sum_none", {"rollup": "sum", "period": "month"}, [{}, None], "none yet"),
])
def test_aggregate_rollup(_id, spec, values, want):
    assert view.aggregate_rollup("r", spec, values)["value"] == want


def test_the_tile_for_an_unknown_app_is_404(pod):
    assert pod["c"].get("/api/apps/no-such-app/records/tile").status_code == 404


# ── replacement ──────────────────────────────────────────────────────────────


def test_a_replaced_screen_redirects_and_the_rest_stays_platform_rendered(pod):
    store = json.loads(json.dumps(STORE))
    store["tables"]["games"]["screens"] = {"list": "/my/games"}
    write_spec(_spec(store), pod["shared"])
    _put(pod, "g1", "Alpha")
    r = pod["c"].get(f"/apps/{APP}/records/games")
    assert r.status_code == 302 and r.headers["Location"] == "/my/games"
    assert _get(pod, f"/apps/{APP}/records/games/g1")[0] == 200
    _, index = _get(pod, f"/apps/{APP}/records")
    assert 'href="/my/games"' in index


# ── auth: 404, never 403 ─────────────────────────────────────────────────────


def test_another_instance_without_a_store_is_404_not_403(pod):
    for path in (f"/apps/{APP}/records/games?instance={OTHER}",
                 f"/apps/{APP}/records/games/g1?instance={OTHER}",
                 f"/apps/{APP}/records?instance={OTHER}"):
        assert _get(pod, path)[0] == 404


def test_a_forbidden_from_the_verbs_surfaces_as_404(pod, monkeypatch):
    """The verbs say ``forbidden`` for a caller outside an instance's audience; the
    surface must not turn that into a 403 that confirms the instance exists."""
    def deny(*a, **k):
        raise app_store.RecordsRefusal("forbidden", "bot 'x' has no binding")
    monkeypatch.setattr(app_store, "records_list", deny)
    code, body = _get(pod, f"/apps/{APP}/records/games")
    assert code == 404 and "binding" not in body


def test_unknown_app_and_unknown_table_are_404(pod):
    assert _get(pod, "/apps/nope/records")[0] == 404
    assert _get(pod, f"/apps/{APP}/records/nope")[0] == 404


def test_several_instances_ask_which_to_show_and_links_carry_it(pod):
    app_store.ensure_store(pod["shared"], APP, OTHER, ass.validate_store(STORE))
    _put(pod, "g1", "Alpha")
    code, body = _get(pod, f"/apps/{APP}/records/games")
    assert code == 200 and f"?instance={BOT}" in body and f"?instance={OTHER}" in body
    _, listing = _get(pod, f"/apps/{APP}/records/games?instance={BOT}")
    assert f"records/games/g1?instance={BOT}" in listing
    _, other = _get(pod, f"/apps/{APP}/records/games?instance={OTHER}")
    assert "Alpha" not in other


# ── fail closed ──────────────────────────────────────────────────────────────


def test_a_schema_the_loader_refuses_renders_not_readable_with_the_reason(pod):
    bad = json.loads(json.dumps(STORE))
    bad["tables"]["games"]["rollups"]["current_status"]["map"] = {"stolen": "x"}
    write_spec(_spec(bad), pod["shared"])
    code, body = _get(pod, f"/apps/{APP}/records/games")
    assert code == 409 and "not readable" in body and "stolen" in body
    assert "Traceback" not in body
    tile = pod["c"].get(f"/api/apps/{APP}/records/tile").get_json()
    assert tile["readable"] is False and "stolen" in tile["reason"]


def test_an_unmigrated_store_renders_not_readable(pod):
    grown = json.loads(json.dumps(STORE))
    grown["tables"]["games"]["columns"]["rating"] = "int"
    write_spec(_spec(grown), pod["shared"])  # spec moved, install has not migrated the file
    code, body = _get(pod, f"/apps/{APP}/records/games")
    assert code == 409 and "not readable" in body and "migrated" in body


def test_no_model_is_reachable_from_the_surface():
    """Reads only through the verbs; nothing on this surface imports a model client."""
    root = _ADMIN_DIR / "evolve_admin"
    for rel in ("app_records_view.py", "web/app_records_routes.py"):
        text = (root / rel).read_text()
        for needle in ("anthropic", "openai", "sqlite3", "llm", "complete("):
            assert needle not in text, (rel, needle)


# ── the directory is the first table shown ───────────────────────────────────


def test_the_directory_renders_on_the_generic_surface(tmp_path):
    from evolve_admin.user_directory import records as drec

    shared = tmp_path / "evolve"
    shared.mkdir()
    (tmp_path / "network.json").write_text(json.dumps({"sharedDir": str(shared)}))
    app_store.ensure_store(shared, drec.DIRECTORY_APP_ID, "pa-bot", drec.APP.schema)
    app_store.records_put(shared, drec.DIRECTORY_APP_ID, "people",
                          {"identity_key": "email:sam@example.com", "person_id": "p1",
                           "names": ["Sam"]},
                          caller=app_store.PLATFORM, instance="pa-bot")
    flask_app = Flask(__name__)
    register_app_records_routes(flask_app, tmp_path / "network.json")
    c = flask_app.test_client()
    r = c.get("/apps/evolve.directory/records/people")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "email:sam@example.com" in body and "resp-table" in body
    assert c.get("/apps/evolve.directory/records/contacts_seen").status_code == 200
    tile = c.get("/api/apps/evolve.directory/records/tile").get_json()
    assert tile["tiles"][0]["rows"] == 1
