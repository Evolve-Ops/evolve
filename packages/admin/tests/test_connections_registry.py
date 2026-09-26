"""test_connections_registry.py — connections.py round-trip, locking, verbs_for.

Every test passes an explicit ``path=`` (a tmp_path file) — never the
module-level ``CONNECTIONS_PATH`` default, which points at the real pod's
shared dir (evolve_dev_not_test_pod: build Evolve, don't touch the test
pod's on-disk state from a unit test).
"""
from __future__ import annotations

import concurrent.futures
import json

from evolve_admin import connections as conn


def _row(bot_id="lex", service="google", role="own", capabilities=None):
    if capabilities is None:
        capabilities = ["calendar.read"]
    return conn.new_connection(
        bot_id=bot_id,
        service=service,
        account_label=f"{bot_id}@example.com",
        role=role,
        jobs=["read_person_calendar"],
        capabilities_=capabilities,
        credential_ref=f"google_integration:{bot_id}",
        credential_kind="service_account_dwd",
    )


class TestRoundTrip:
    def test_save_then_load_returns_the_same_rows(self, tmp_path):
        path = tmp_path / "connections.json"
        row = _row()
        conn.save_connections({"connections": [row]}, path=path)
        loaded = conn.load_connections(path=path)
        assert loaded["connections"] == [row]

    def test_missing_file_loads_as_empty(self, tmp_path):
        path = tmp_path / "does-not-exist.json"
        assert conn.load_connections(path=path) == {"connections": []}

    def test_corrupt_file_loads_as_empty_not_a_crash(self, tmp_path):
        path = tmp_path / "connections.json"
        path.write_text("{not valid json")
        assert conn.load_connections(path=path) == {"connections": []}

    def test_save_is_atomic_no_leftover_tmp_file(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": [_row()]}, path=path)
        leftovers = list(tmp_path.glob(".connections-*"))
        assert leftovers == []

    def test_add_connection_appends_under_the_lock(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": [_row("lex")]}, path=path)
        conn.add_connection(_row("rex"), path=path)
        data = conn.load_connections(path=path)
        assert {c["bot_id"] for c in data["connections"]} == {"lex", "rex"}

    def test_add_connection_remints_a_colliding_id(self, tmp_path):
        path = tmp_path / "connections.json"
        first = _row("lex")
        conn.save_connections({"connections": [first]}, path=path)
        colliding = _row("rex")
        colliding["id"] = first["id"]
        conn.add_connection(colliding, path=path)
        data = conn.load_connections(path=path)
        ids = [c["id"] for c in data["connections"]]
        assert len(ids) == len(set(ids)) == 2


class TestConcurrentAdd:
    def test_parallel_add_connection_loses_nothing(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": []}, path=path)
        bot_ids = [f"bot-{i}" for i in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda b: conn.add_connection(_row(b), path=path), bot_ids))
        data = conn.load_connections(path=path)
        assert {c["bot_id"] for c in data["connections"]} == set(bot_ids)
        assert len(data["connections"]) == len(bot_ids)


class TestForBot:
    def test_for_bot_filters_by_bot_id(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections(
            {"connections": [_row("lex"), _row("rex"), _row("lex", service="github",
                                                              capabilities=["repo.push_backup"])]},
            path=path,
        )
        rows = conn.for_bot("lex", path=path)
        assert len(rows) == 2
        assert {r["service"] for r in rows} == {"google", "github"}


class TestVerbsForRole:
    def test_own_role_yields_mutating_verbs(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections(
            {"connections": [_row("lex", role="own", capabilities=["calendar.create"])]},
            path=path,
        )
        assert conn.verbs_for("lex", "google", path=path) == ["calendar.create"]

    def test_user_role_never_yields_a_mutating_verb(self, tmp_path):
        # D-GA2: a delegated human account is read-only by construction,
        # even if the stored row (hand-edited, or a future migration bug)
        # lists a mutating verb — the capability map's own `mutates` flag
        # is the enforcement source of truth, not the row.
        path = tmp_path / "connections.json"
        conn.save_connections(
            {"connections": [_row("lex", role="user",
                                   capabilities=["calendar.read", "calendar.create"])]},
            path=path,
        )
        verbs = conn.verbs_for("lex", "google", path=path)
        assert verbs == ["calendar.read"]
        assert "calendar.create" not in verbs

    def test_unknown_verb_in_row_is_ignored_not_crashed(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections(
            {"connections": [_row("lex", capabilities=["calendar.read", "made.up.verb"])]},
            path=path,
        )
        assert conn.verbs_for("lex", "google", path=path) == ["calendar.read"]

    def test_no_rows_yields_empty_list(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": []}, path=path)
        assert conn.verbs_for("lex", "google", path=path) == []


class TestIsConfiguredViaRegistry:
    def test_no_row_returns_none(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": []}, path=path)
        assert conn.is_configured_via_registry("lex", "google", path=path) is None

    def test_row_with_capabilities_returns_true(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections({"connections": [_row("lex")]}, path=path)
        assert conn.is_configured_via_registry("lex", "google", path=path) is True

    def test_row_with_no_capabilities_returns_false(self, tmp_path):
        path = tmp_path / "connections.json"
        conn.save_connections(
            {"connections": [_row("lex", capabilities=[])]}, path=path,
        )
        assert conn.is_configured_via_registry("lex", "google", path=path) is False


class TestHealthDisplay:
    def test_unknown_renders_not_yet_verified(self):
        assert conn.health_display(conn.new_health("unknown")) == "not yet verified"

    def test_unknown_never_renders_a_check_mark(self):
        assert "✓" not in conn.health_display(conn.new_health("unknown"))

    def test_verified_renders_verified(self):
        assert conn.health_display(conn.new_health("verified")) == "verified"

    def test_degraded_includes_the_reason(self):
        h = conn.new_health("degraded", reason="update: 403 insufficient permission")
        assert conn.health_display(h) == "degraded: update: 403 insufficient permission"

    def test_missing_health_dict_renders_not_yet_verified(self):
        assert conn.health_display(None) == "not yet verified"
