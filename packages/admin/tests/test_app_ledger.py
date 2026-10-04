"""Ledger entries, rollups from entries, and the D-CS7 status control (D-AD3).

Module: ``evolve_admin/app_ledger.py`` (+ ``app_store_cli.py`` for the doctor).
Brief: ``app-store-and-ledger-verbs`` items 3, 4 and 6.

WHAT THESE PIN:
  * ``ledger.append`` takes exactly the platform's entry shape, a ``kind`` from
    the app's closed set, and a thing that exists — else a typed refusal and
    no entry.
  * Rollups (status, holder, last event, count, per-period sums) are DERIVED
    from entries on every read; nothing about them is stored.
  * A stored status that no entry explains fails the doctor for that app —
    and a store the doctor cannot read is UNKNOWN, never OK.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_ledger, app_store  # noqa: E402
from evolve_admin import app_spec_schema as ass  # noqa: E402
from evolve_admin.app_store import RecordsRefusal  # noqa: E402
from evolve_admin.applications.app_spec import AppSpec  # noqa: E402
from evolve_admin.applications.app_spec_store import write_spec  # noqa: E402

APP = "collection-tracker"
BOT = "bot-a"

STORE = {
    "tables": {
        "games": {
            "columns": {"id": "text", "title": "text", "status": "text"},
            "key": ["id"],
            "rollups": {
                "current_status": {"rollup": "status", "column": "status",
                                   "map": {"acquired": "owned", "sold": "gone",
                                           "loaned": "on_loan", "returned": "owned"}},
                "current_holder": {"rollup": "holder", "set_by": ["loaned"],
                                   "cleared_by": ["returned", "sold"]},
                "last_event_at": {"rollup": "last_event_at"},
                "plays": {"rollup": "count", "kinds": ["played"]},
                "spend_by_month": {"rollup": "sum", "period": "month",
                                   "kinds": ["acquired"]},
                "net": {"rollup": "sum", "period": "all"},
            },
        },
        "events": {"ledger": True, "of": "games",
                   "kinds": ["acquired", "sold", "loaned", "returned", "played"]},
    },
}


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    write_spec(AppSpec.from_dict({"app_id": APP, "name": "n", "purpose": "p",
                                  "store": STORE}), s)
    app_store.ensure_store(s, APP, BOT, ass.validate_store(STORE))
    app_store.records_put(s, APP, "games", {"id": "g1", "title": "A"}, caller=BOT)
    return s


def _append(shared, **entry):
    entry.setdefault("thing_id", "g1")
    return app_ledger.ledger_append(shared, APP, entry, caller=BOT)


def _entries(shared) -> int:
    with sqlite3.connect(app_store.store_path(shared, APP, BOT)) as c:
        return c.execute('SELECT COUNT(*) FROM "events"').fetchone()[0]


# ── append ──────────────────────────────────────────────────────────────────


def test_append_records_the_platform_shape_and_stamps_by(shared):
    out = _append(shared, kind="acquired", at="2026-09-01", amount=30,
                  counterparty="a shop", note="birthday")
    e = out["entry"]
    assert out["ledger"] == "events" and e["seq"] == 1
    assert e == {"seq": 1, "thing_id": "g1", "kind": "acquired", "at": "2026-09-01",
                 "by": f"bot:{BOT}", "amount": 30.0, "counterparty": "a shop",
                 "note": "birthday"}
    assert _append(shared, kind="loaned", at="2026-09-02T18:00:00Z",
                   by="person:the-owner")["entry"]["by"] == "person:the-owner"


@pytest.mark.parametrize("entry, code", [
    ({"kind": "stolen", "at": "2026-09-01"}, "unknown_kind"),
    ({"kind": "sold"}, "missing_field"),
    ({"kind": "sold", "at": "2026-09-01", "thing_id": ""}, "missing_field"),
    ({"kind": "sold", "at": "yesterday"}, "bad_value"),
    ({"kind": "sold", "at": "2026-09-01", "amount": "ten"}, "bad_value"),
    ({"kind": "sold", "at": "2026-09-01", "amount": True}, "bad_value"),
    ({"kind": "sold", "at": "2026-09-01", "price": 10}, "undeclared_column"),
    ({"kind": "sold", "at": "2026-09-01", "thing_id": "ghost"}, "unknown_thing"),
    ({"kind": "sold", "at": "2026-09-01", "note": "x" * 3000}, "bad_value"),
])
def test_a_bad_entry_is_refused_typed_and_nothing_is_appended(shared, entry, code):
    with pytest.raises(RecordsRefusal) as exc:
        _append(shared, **entry)
    assert exc.value.code == code
    assert _entries(shared) == 0


def test_an_app_without_a_ledger_refuses_append(tmp_path):
    store = {"tables": {"t": {"columns": {"id": "text"}, "key": ["id"]}}}
    write_spec(AppSpec.from_dict({"app_id": "plain-app", "store": store}), tmp_path)
    app_store.ensure_store(tmp_path, "plain-app", BOT, ass.validate_store(store))
    with pytest.raises(RecordsRefusal) as exc:
        app_ledger.ledger_append(tmp_path, "plain-app", {"thing_id": "x", "kind": "a",
                                                         "at": "2026-09-01"}, caller=BOT)
    assert exc.value.code == "no_ledger"


def test_a_thing_with_entries_cannot_be_deleted(shared):
    _append(shared, kind="acquired", at="2026-09-01")
    with pytest.raises(RecordsRefusal) as exc:
        app_store.records_delete(shared, APP, "games", "g1", caller=BOT)
    assert exc.value.code == "has_entries"
    assert "append a closing entry" in exc.value.message


# ── rollups: computed from entries ──────────────────────────────────────────


def test_get_derives_status_holder_last_event_counts_and_sums(shared):
    got = app_store.records_get(shared, APP, "games", "g1", caller=BOT)
    assert got["rollups"] == {"current_status": None, "current_holder": None,
                              "last_event_at": None, "plays": 0,
                              "spend_by_month": {}, "net": {}}
    _append(shared, kind="acquired", at="2026-08-20", amount=30)
    _append(shared, kind="acquired", at="2026-09-03", amount=12.5)
    _append(shared, kind="played", at="2026-09-04")
    _append(shared, kind="loaned", at="2026-09-05", counterparty="a friend")
    r = app_store.records_get(shared, APP, "games", "g1", caller=BOT)["rollups"]
    assert r["current_status"] == "on_loan"
    assert r["current_holder"] == "a friend"
    assert r["last_event_at"] == "2026-09-05"
    assert r["plays"] == 1
    assert r["spend_by_month"] == {"2026-08": 30.0, "2026-09": 12.5}
    assert r["net"] == {"all": 42.5}
    _append(shared, kind="returned", at="2026-09-09")
    r = app_store.records_get(shared, APP, "games", "g1", caller=BOT)["rollups"]
    assert (r["current_status"], r["current_holder"]) == ("owned", None)


def test_rollups_order_by_when_it_happened_not_when_it_was_recorded(shared):
    _append(shared, kind="sold", at="2026-09-10")
    _append(shared, kind="acquired", at="2026-01-01")  # back-filled later
    r = app_store.records_get(shared, APP, "games", "g1", caller=BOT)["rollups"]
    assert r["current_status"] == "gone"


def test_nothing_derived_is_stored(shared):
    _append(shared, kind="loaned", at="2026-09-05", counterparty="a friend")
    row = app_store.records_get(shared, APP, "games", "g1", caller=BOT)["row"]
    assert row["status"] is None  # the stored column is untouched by entries
    with sqlite3.connect(app_store.store_path(shared, APP, BOT)) as c:
        cols = [r[1] for r in c.execute('PRAGMA table_info("games")')]
    assert "current_status" not in cols and "current_holder" not in cols


def test_history_carries_the_entries(shared):
    _append(shared, kind="acquired", at="2026-09-01")
    _append(shared, kind="loaned", at="2026-09-02", counterparty="a friend")
    h = app_store.records_history(shared, APP, "games", "g1", caller=BOT)
    assert [e["kind"] for e in h["entries"]] == ["acquired", "loaned"]
    assert [r["op"] for r in h["revisions"]] == ["insert"]


# ── the D-CS7 control ───────────────────────────────────────────────────────


def test_a_stored_status_no_entry_explains_fails(shared):
    app_store.records_put(shared, APP, "games", {"id": "g2", "status": "owned"}, caller=BOT)
    app_store.records_put(shared, APP, "games", {"id": "g1", "status": "gone"}, caller=BOT)
    _append(shared, kind="acquired", at="2026-09-01")  # g1: entries say owned
    app_store.records_put(shared, APP, "games", {"id": "g3"}, caller=BOT)  # no status: fine
    out = app_ledger.unexplained_statuses(shared, APP, BOT)
    assert out["status"] == "fail"
    by_key = {f["key"]: f for f in out["findings"]}
    assert set(by_key) == {"g1", "g2"}
    assert by_key["g1"]["stored"] == "gone" and by_key["g1"]["derived"] == "owned"
    assert by_key["g2"]["why"] == "no entry at all"


def test_explained_statuses_pass(shared):
    _append(shared, kind="acquired", at="2026-09-01")
    app_store.records_put(shared, APP, "games", {"id": "g1", "status": "owned"}, caller=BOT)
    assert app_ledger.unexplained_statuses(shared, APP, BOT)["status"] == "ok"


def test_the_control_reports_unknown_when_it_cannot_look(shared, monkeypatch):
    grown = json.loads(json.dumps(STORE))
    grown["tables"]["games"]["columns"]["x"] = "text"
    write_spec(AppSpec.from_dict({"app_id": APP, "store": grown}), shared)
    out = app_ledger.unexplained_statuses(shared, APP, BOT)
    assert out["status"] == "unknown" and "schema_mismatch" in out["reason"]
    write_spec(AppSpec.from_dict({"app_id": APP, "store": STORE}), shared)
    import os
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    out = app_ledger.unexplained_statuses(shared, APP, BOT)
    assert out["status"] == "unknown" and "owned by uid" in out["reason"]
    assert app_ledger.unexplained_statuses(shared, APP, "no-such-bot")["status"] == "unknown"


def test_records_doctor_cli_exits_by_the_worst_result(shared, tmp_path):
    from evolve_admin.cli import main
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(shared), "bots": {}}))
    runner = CliRunner()
    ok = runner.invoke(main, ["--network", str(net), "records", "doctor"])
    assert ok.exit_code == 0, ok.output
    assert f"OK      {APP}/{BOT}" in ok.output
    app_store.records_put(shared, APP, "games", {"id": "g1", "status": "gone"}, caller=BOT)
    bad = runner.invoke(main, ["--network", str(net), "records", "doctor"])
    assert bad.exit_code == 1, bad.output
    assert "games[g1].status = 'gone', entries say None (no entry at all)" in bad.output
    js = runner.invoke(main, ["--network", str(net), "records", "doctor", "--json",
                              "--app", APP])
    assert json.loads(js.output)[0]["status"] == "fail"
    none = runner.invoke(main, ["--network", str(net), "records", "doctor", "--app", "other"])
    assert none.exit_code == 0 and "no app stores" in none.output
