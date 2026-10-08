"""test_connections_grant_scope.py — D-TF3: a consent's LIFETIME (grant_scope).

Placeholder names throughout; no credential is read or moved.
"""
from __future__ import annotations

import json

import pytest

from evolve_admin import connections as conn
from evolve_admin import connections_migration as mig


def _row(caps_, scope=None, jobs=None, **kw):
    return conn.new_connection(
        bot_id="lex", service="google", account_label="lex@example.com", role="own",
        jobs=jobs or [], capabilities_=caps_, credential_ref="google_integration:lex",
        credential_kind="service_account_dwd", grant_scope=scope, **kw)


def _save(tmp_path, *rows):
    p = tmp_path / "connections.json"
    conn.save_connections({"connections": list(rows)}, p)
    return p


class TestScopeValidation:
    @pytest.mark.parametrize("bad", ["forever", "until:", "until:tomorrow", "", "Standing"])
    def test_unknown_scope_is_refused_by_new_connection(self, bad):
        with pytest.raises(conn.InvalidGrantScope):
            _row(["gmail.read"], scope=bad)

    def test_unknown_scope_on_disk_is_refused_at_load_never_standing(self, tmp_path):
        row = _row(["gmail.read"])
        row["grant_scope"] = "forever"
        p = tmp_path / "connections.json"
        p.write_text(json.dumps({"connections": [row]}))
        with pytest.raises(conn.InvalidGrantScope, match=row["id"]):
            conn.load_connections(p)

    @pytest.mark.parametrize("ok", ["once", "task", "standing", "until:2026-10-01T00:00:00Z"])
    def test_the_four_shapes_are_accepted(self, ok):
        assert _row(["gmail.read"], scope=ok)["grant_scope"] == ok


class TestDefaults:
    def test_read_only_row_defaults_standing(self):
        assert _row(["gmail.read", "calendar.read"])["grant_scope"] == "standing"

    @pytest.mark.parametrize("verb", ["gmail.send", "calendar.create", "calendar.update",
                                      "calendar.delete", "drive.write"])
    def test_any_send_or_spend_verb_defaults_task(self, verb):
        assert _row(["gmail.read", verb])["grant_scope"] == "task"

    def test_a_job_naming_a_mutating_verb_defaults_task(self):
        assert _row([], jobs=["send_mail_as_itself"])["grant_scope"] == "task"

    def test_github_write_defaults_task(self):
        r = conn.new_connection(
            bot_id="lex", service="github", account_label="org/repo", role="own",
            capabilities_=["repo.push_backup"], credential_ref="k", credential_kind="pat")
        assert r["grant_scope"] == "task"


class TestExpiry:
    def test_standing_row_is_unchanged(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.read"]))
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.read"]
        row = conn.load_connections(p)["connections"][0]
        assert conn.effective_health(row)["state"] == "unknown"
        assert conn.grant_scope_display(row) == "standing"

    def test_lapsed_until_yields_nothing_and_reads_expired(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.read"], scope="until:2020-01-01T00:00:00Z"))
        assert conn.verbs_for("lex", "google", path=p) == []
        row = conn.load_connections(p)["connections"][0]
        assert conn.effective_health(row)["state"] == "expired"
        assert conn.grant_scope_display(row) == "expired"

    def test_future_until_still_grants_and_shows_the_date(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.read"], scope="until:2999-10-01T00:00:00Z"))
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.read"]
        assert conn.grant_scope_display(conn.load_connections(p)["connections"][0]) == "until 2999-10-01"

    @pytest.mark.parametrize("scope", ["once", "task"])
    def test_spent_once_and_task_yield_nothing(self, tmp_path, scope):
        r = _row(["gmail.send"], scope=scope)
        r["spent_at"] = "2026-09-30T00:00:00Z"
        p = _save(tmp_path, r)
        assert conn.verbs_for("lex", "google", path=p) == []
        assert conn.effective_health(r)["state"] == "expired"

    def test_row_without_the_field_is_unscoped_at_read_time_and_not_stamped(self, tmp_path):
        r = _row(["gmail.send"])
        del r["grant_scope"], r["granted_at"], r["migrated"]
        p = _save(tmp_path, r)
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.send"]
        assert "grant_scope" not in conn.load_connections(p)["connections"][0]


class TestSpend:
    def test_once_is_spent_on_first_spend_verb(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.send"], scope="once"))
        conn.record_spend("lex", "google", "gmail.send", None, p)
        assert conn.load_connections(p)["connections"][0]["spent_at"]
        assert conn.verbs_for("lex", "google", path=p) == []

    def test_a_read_verb_never_spends(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.read", "gmail.send"], scope="once"))
        conn.record_spend("lex", "google", "gmail.read", None, p)
        assert "spent_at" not in conn.load_connections(p)["connections"][0]

    def test_task_stays_open_for_its_run_and_is_spent_when_a_new_run_appears(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.send"], scope="task"))
        conn.record_spend("lex", "google", "gmail.send", "run-1", p)
        conn.settle_prior_runs("lex", "google", "run-1", p)
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.send"]
        conn.settle_prior_runs("lex", "google", "run-2", p)
        assert conn.verbs_for("lex", "google", path=p) == []

    def test_task_with_no_run_id_is_never_spent(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.send"], scope="task"))
        for _ in range(2):
            conn.record_spend("lex", "google", "gmail.send", None, p)
            assert "spent_at" not in conn.load_connections(p)["connections"][0]
            assert conn.verbs_for("lex", "google", path=p) == ["gmail.send"]

    def test_spend_is_scoped_to_the_named_row(self, tmp_path):
        a, b = _row(["gmail.send"], scope="once"), _row(["gmail.send"], scope="once")
        p = _save(tmp_path, a, b)
        conn.record_spend("lex", "google", "gmail.send", None, p, account_id=a["id"])
        rows = {r["id"]: r for r in conn.load_connections(p)["connections"]}
        assert rows[a["id"]].get("spent_at") and not rows[b["id"]].get("spent_at")
        assert conn.verbs_for("lex", "google", account_id=b["id"], path=p) == ["gmail.send"]

    def test_end_task_run_spends_that_runs_grant(self, tmp_path):
        p = _save(tmp_path, _row(["gmail.send"], scope="task"))
        conn.record_spend("lex", "google", "gmail.send", "run-1", p)
        conn.end_task_run("lex", "google", "run-1", p)
        assert conn.verbs_for("lex", "google", path=p) == []

    def test_make_standing_revives_a_spent_row_and_expire_now_kills_one(self, tmp_path):
        r = _row(["gmail.send"], scope="once")
        p = _save(tmp_path, r)
        conn.record_spend("lex", "google", "gmail.send", None, p)
        assert conn.set_grant_scope(r["id"], "standing", p)
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.send"]
        assert conn.expire_now(r["id"], p)
        assert conn.verbs_for("lex", "google", path=p) == []


class TestMigration:
    NET = {"bots": {"lex": {"google_integration": {
        "mode": "service_account_dwd",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"]}}}}

    def test_new_rows_are_marked_migrated_and_defaulted(self):
        row = mig.build_migrated_rows(self.NET)[0]
        assert row["migrated"] is True
        assert row["grant_scope"] == "task"

    def test_stamp_defaults_legacy_rows_once_and_marks_migrated(self, tmp_path):
        send, read = _row(["gmail.send"]), _row(["gmail.read"])
        for r in (send, read):
            del r["grant_scope"], r["granted_at"], r["migrated"]
        p = _save(tmp_path, send, read)
        assert mig.stamp_grant_scopes(p) == 2
        rows = conn.load_connections(p)["connections"]
        assert [(r["grant_scope"], r["migrated"]) for r in rows] == [("standing", True), ("standing", True)]
        # a pre-D-TF3 send row grants exactly what it granted on main, and the
        # tile can still say "defaulted, not chosen"
        assert conn.verbs_for("lex", "google", path=p) == ["gmail.send", "gmail.read"]
        assert all(r["migrated"] for r in rows)
        assert mig.stamp_grant_scopes(p) == 0

    def test_stamp_never_touches_a_chosen_row(self, tmp_path):
        r = _row(["gmail.send"], scope="standing")
        p = _save(tmp_path, r)
        assert mig.stamp_grant_scopes(p) == 0
        assert conn.load_connections(p)["connections"][0]["migrated"] is False

    def test_stamp_rewrites_a_pre_tf3_imessage_row_the_loader_refuses(self, tmp_path):
        # #4667 wrote the iMessage consent scope into grant_scope before D-TF3
        # gave the field its lifetime meaning; such a file must not brick the
        # registry once this lands.
        r = conn.new_connection(
            bot_id="lex", service="imessage", account_label="+15550100", role="own",
            capabilities_=["imessage.receive", "imessage.send"],
            credential_ref="messages:lex", credential_kind="messages_signin")
        del r["granted_at"], r["migrated"]
        p = tmp_path / "connections.json"
        p.write_text(json.dumps({"connections": [dict(r, grant_scope="channel:imessage")]}))
        with pytest.raises(conn.InvalidGrantScope):
            conn.load_connections(p)
        assert mig.stamp_grant_scopes(p) == 1
        row = conn.load_connections(p)["connections"][0]
        assert (row["grant_scope"], row["migrated"]) == ("standing", True)
        assert row["granted_at"] == row["added_at"]
        assert conn.verbs_for("lex", "imessage", path=p) == ["imessage.receive", "imessage.send"]
        assert mig.stamp_grant_scopes(p) == 0


from click.testing import CliRunner  # noqa: E402

from evolve_admin.cli import main as _cli_main  # noqa: E402


def test_cli_expire_then_revive(tmp_path):
    r = _row(["gmail.send"], scope="standing")
    p = _save(tmp_path, r)
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(p.parent)}))
    run = CliRunner().invoke
    assert run(_cli_main, ["connections", "expire", r["id"], "--network", str(net)]).exit_code == 0
    assert conn.verbs_for("lex", "google", path=p) == []
    assert run(_cli_main, ["connections", "revive", r["id"], "--network", str(net)]).exit_code == 0
    assert conn.verbs_for("lex", "google", path=p) == ["gmail.send"]
    assert run(_cli_main, ["connections", "revive", "nope", "--network", str(net)]).exit_code != 0
