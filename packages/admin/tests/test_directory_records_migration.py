"""The directory on the records layer — its first consumer (brief item 5).

Modules: ``evolve_admin/user_directory/records.py`` + ``storage.py``. Brief:
``app-store-and-ledger-verbs``.

WHAT THESE PIN:
  * **Behaviour unchanged**: a pod whose directory lived in
    ``directory/<bot>.json`` resolves the same Persons after the move.
  * **Read once**: the JSON is imported in one transaction when the store is
    created, the marker records its path + sha256, the file is left in place
    byte-for-byte, and a later edit to it is NOT imported over newer rows.
  * **Only the owner migrates**: a process that may not create the store keeps
    reading the JSON exactly as before, and refuses to write.
  * Every write lands as a ``people`` row PLUS a ``contacts_seen`` entry whose
    kind says what kind of write it was.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import app_store  # noqa: E402
from evolve_admin.app_store import PLATFORM  # noqa: E402
from evolve_admin.user_directory import records as udr_store  # noqa: E402
from evolve_admin.user_directory import resolver as udr  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402

BOT = "team_bot_a"

LEGACY = {
    "bot_id": BOT, "version": 1,
    "persons": {
        "slack:U0FAKEUSR1": {
            "person_id": "pers_aaaaaaaaaaaaaaaa",
            "emails": [{"addr": "first@example.test", "rank": "primary",
                        "provenance": "operator-verified", "verified": True}],
            "names": {"display": "First Person"},
            "audit": [{"field": "emails", "from": None, "to": [], "by": "op",
                       "source": "operator-verified", "at": "2026-08-01T00:00:00Z"}],
            "future_field": {"kept": True},
        },
        "telegram:111": {"person_id": "pers_bbbbbbbbbbbbbbbb",
                         "contact": {"note": "met at the fair"}, "profile_ref": 7},
    },
}


def _seed_legacy(shared: Path, data: dict = LEGACY) -> Path:
    p = shared / "directory" / f"{BOT}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    return p


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _ledger(shared: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(udr_store.store_path(shared, BOT)) as c:
        return list(c.execute('SELECT thing_id, kind, by FROM "contacts_seen" ORDER BY seq'))


def test_the_json_is_imported_once_and_left_in_place(shared):
    legacy = _seed_legacy(shared)
    raw = legacy.read_bytes()
    d = uds.load_directory(shared, BOT)

    assert d["persons"] == {
        "slack:U0FAKEUSR1": LEGACY["persons"]["slack:U0FAKEUSR1"],
        "telegram:111": LEGACY["persons"]["telegram:111"],
    }
    assert udr_store.store_path(shared, BOT).exists()
    assert legacy.read_bytes() == raw  # left exactly where it was
    assert sorted(_ledger(shared)) == [("slack:U0FAKEUSR1", "imported", "migration"),
                                       ("telegram:111", "imported", "migration")]
    with app_store.open_for_verb(shared, udr_store.DIRECTORY_APP_ID, PLATFORM, "list",
                                 BOT) as (conn, _a, _i):
        marker = json.loads(app_store.read_meta(conn, "legacy_import") or "{}")
    assert marker["sha256"] == hashlib.sha256(raw).hexdigest()
    assert marker["persons"] == 2 and marker["present"] is True


def test_a_later_edit_to_the_json_is_never_imported_over_the_table(shared):
    legacy = _seed_legacy(shared)
    uds.load_directory(shared, BOT)
    uds.upsert_entry(shared, BOT, "telegram", "111", by="bot", provenance="bot-asserted",
                     contact={"note": "newer"})
    stale = json.loads(legacy.read_text())
    stale["persons"]["telegram:111"]["contact"] = {"note": "stale"}
    stale["persons"]["slack:U0NEW"] = {"person_id": "pers_cccccccccccccccc"}
    legacy.write_text(json.dumps(stale))
    udr_store._READY.clear()  # noqa: SLF001 — a fresh daemon process
    d = uds.load_directory(shared, BOT)
    assert d["persons"]["telegram:111"]["contact"] == {"note": "newer"}
    assert "slack:U0NEW" not in d["persons"]


def test_resolution_is_unchanged_by_the_move(shared):
    net = {"sharedDir": str(shared), "bots": {BOT: {}}}
    _seed_legacy(shared)
    # Before: a non-owner process reads the JSON (the pre-migration world).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(udr_store, "is_store_owner", lambda s: False)
        before = udr.resolve_persons(net, BOT)
    assert not udr_store.store_path(shared, BOT).exists()
    after = udr.resolve_persons(net, BOT)  # the daemon: migrates, reads the table
    assert udr_store.store_path(shared, BOT).exists()
    assert [p.to_dict() for p in before] == [p.to_dict() for p in after]


def test_a_non_owner_reads_the_json_and_refuses_to_write(shared, monkeypatch):
    _seed_legacy(shared)
    monkeypatch.setattr(udr_store, "is_store_owner", lambda s: False)
    assert set(uds.load_directory(shared, BOT)["persons"]) == set(LEGACY["persons"])
    with pytest.raises(PermissionError):
        uds.upsert_entry(shared, BOT, "slack", "U0FAKEUSR1", by="x",
                         provenance="bot-asserted", contact={"a": 1})
    assert not udr_store.store_path(shared, BOT).exists()


def test_reading_a_bot_with_no_directory_creates_nothing(shared):
    assert uds.load_directory(shared, BOT)["persons"] == {}
    assert not (shared / "apps").exists()


def test_an_unparseable_json_is_left_in_place_and_named(shared):
    p = shared / "directory" / f"{BOT}.json"
    p.parent.mkdir(parents=True)
    p.write_text("{broken")
    assert uds.load_directory(shared, BOT)["persons"] == {}
    assert p.read_text() == "{broken"
    with app_store.open_for_verb(shared, udr_store.DIRECTORY_APP_ID, PLATFORM, "list",
                                 BOT) as (conn, _a, _i):
        marker = json.loads(app_store.read_meta(conn, "legacy_import") or "{}")
    assert marker["problem"].startswith("unparseable")


def test_a_failed_import_is_never_read_as_an_empty_directory(shared, monkeypatch):
    _seed_legacy(shared)
    real = udr_store._import_legacy  # noqa: SLF001

    def boom(*a, **kw):
        raise RuntimeError("disk hiccup")

    monkeypatch.setattr(udr_store, "_import_legacy", boom)
    # The store file now exists but carries no marker: reads fall back to JSON.
    assert set(uds.load_directory(shared, BOT)["persons"]) == set(LEGACY["persons"])
    with pytest.raises(RuntimeError):
        uds.upsert_entry(shared, BOT, "slack", "U0X", by="x", provenance="bot-asserted",
                         contact={"a": 1})
    monkeypatch.setattr(udr_store, "_import_legacy", real)
    assert set(uds.load_directory(shared, BOT)["persons"]) == set(LEGACY["persons"])


def test_each_write_appends_a_contacts_seen_entry_of_its_kind(shared):
    uds.mint_person_id(shared, BOT, "slack", "U0A")
    uds.mint_person_id(shared, BOT, "slack", "U0A")  # already minted: no entry
    uds.upsert_entry(shared, BOT, "slack", "U0A", by="bot", provenance="bot-asserted",
                     contact={"org": "x"})
    uds.upsert_entry(shared, BOT, "slack", "U0A", by="op@test",
                     provenance="operator-verified", names={"display": "A"})
    uds.upsert_entry(shared, BOT, "slack", "U0B", by="slack", provenance="channel-captured",
                     names={"display": "B"})
    assert _ledger(shared) == [
        ("slack:U0A", "minted", "system"),
        ("slack:U0A", "asserted", "bot"),
        ("slack:U0A", "verified", "op@test"),
        ("slack:U0B", "captured", "slack"),
    ]
    got = app_store.records_get(shared, udr_store.DIRECTORY_APP_ID, "people", "slack:U0A",
                                caller=PLATFORM, instance=BOT)
    assert got["rollups"]["times_seen"] == 3
    assert got["rollups"]["last_seen_at"]


def test_concurrent_upserts_to_different_people_both_land(shared):
    errors: list[BaseException] = []

    def write(i: int) -> None:
        try:
            uds.upsert_entry(shared, BOT, "slack", f"U{i:03d}", by="bot",
                             provenance="bot-asserted", contact={"n": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(uds.load_directory(shared, BOT)["persons"]) == 12


def test_the_directory_table_is_reachable_only_by_the_platform(shared):
    uds.mint_person_id(shared, BOT, "slack", "U0A")
    with pytest.raises(app_store.RecordsRefusal) as exc:
        app_store.records_list(shared, udr_store.DIRECTORY_APP_ID, "people", caller="other-bot",
                               instance=BOT)
    assert exc.value.code == "forbidden"
