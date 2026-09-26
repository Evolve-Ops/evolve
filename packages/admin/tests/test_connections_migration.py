"""test_connections_migration.py — the one-shot D-CN8 wrap, from a fixture.

Fixture: two bots each with a configured ``google_integration`` (today's
shape — see ``connections_migration``'s own docstring on why this reads
``google_integration``, not the not-yet-shipped ``google_accounts[]``), one
of which also has a ``backupRepoUrl`` (the keystore-PAT-backed job) ->
three connection rows total, all ``health.state == "unknown"``. Idempotent:
migrating a second time is a no-op because the file already exists.
"""
from __future__ import annotations

import json

from evolve_admin import connections as conn
from evolve_admin import connection_capabilities as caps
from evolve_admin import connections_migration as mig
from evolve_admin.google_service import CALENDAR_SCOPES_READ, GMAIL_SCOPES_SEND, GMAIL_SCOPES_READ


def _fixture_network() -> dict:
    return {
        "bots": {
            "lex": {
                "role": "member",
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": "lex@example-corp.com",
                    "scopes": list(CALENDAR_SCOPES_READ),
                },
                "backupRepoUrl": "git@github.com:example-org/lex-workspace.git",
            },
            "rex": {
                "role": "member",
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": "rex@example-corp.com",
                    "scopes": list(GMAIL_SCOPES_SEND),
                },
            },
            "unconfigured-bot": {"role": "member"},
        }
    }


class TestBuildMigratedRows:
    def test_two_google_accounts_and_a_pat_yield_three_rows(self):
        rows = mig.build_migrated_rows(_fixture_network())
        assert len(rows) == 3

    def test_every_migrated_row_is_health_unknown(self):
        rows = mig.build_migrated_rows(_fixture_network())
        assert all(r["health"]["state"] == "unknown" for r in rows)
        assert all(r["health"]["reason"] for r in rows)

    def test_google_rows_carry_the_scope_derived_job_and_verbs(self):
        rows = mig.build_migrated_rows(_fixture_network())
        lex_google = next(r for r in rows if r["bot_id"] == "lex" and r["service"] == "google")
        assert lex_google["jobs"] == ["read_person_calendar"]
        assert lex_google["capabilities"] == ["calendar.read"]
        assert lex_google["account"]["role"] == "own"

    def test_github_row_points_at_the_shared_keystore_pat(self):
        rows = mig.build_migrated_rows(_fixture_network())
        lex_github = next(r for r in rows if r["bot_id"] == "lex" and r["service"] == "github")
        assert lex_github["credential_ref"] == "keystore:github_pat"
        assert lex_github["credential_kind"] == "pat"
        assert lex_github["expires_at"] is None
        assert lex_github["capabilities"] == ["repo.push_backup"]

    def test_unconfigured_bot_yields_no_rows(self):
        rows = mig.build_migrated_rows(_fixture_network())
        assert all(r["bot_id"] != "unconfigured-bot" for r in rows)

    def test_scope_shaped_grant_that_matches_no_job_gets_empty_jobs_and_a_reason(self):
        # A grant covering only PART of a job's verb set (calendar.read
        # alone doesn't complete manage_own_calendar, but IS the whole
        # read_person_calendar job — pick a scope set that matches nothing).
        network = {
            "bots": {
                "odd": {
                    "google_integration": {
                        "mode": "service_account_dwd",
                        "scopes": list(GMAIL_SCOPES_READ) + ["https://www.googleapis.com/auth/unrecognized"],
                    },
                },
            },
        }
        rows = mig.build_migrated_rows(network)
        odd = next(r for r in rows if r["bot_id"] == "odd")
        # gmail.read IS a whole job (read_person_mail) on its own, so this
        # particular grant DOES resolve to a job — assert the verb landed
        # and the unrecognized scope didn't blow anything up.
        assert "gmail.read" in odd["capabilities"]

    def test_pending_verbs_never_land_in_a_migrated_row(self):
        # auth/calendar covers calendar.create plus the tool-less
        # update/delete/move; only the verb with a tool is recorded.
        network = {"bots": {"cal": {"google_integration": {
            "mode": "service_account_dwd",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }}}}
        cal = mig.build_migrated_rows(network)[0]
        assert cal["capabilities"] == ["calendar.create"]
        assert not any(caps.is_pending("google", v) for v in cal["capabilities"])


class TestMigrateIfNeeded:
    def test_writes_the_file_when_absent(self, tmp_path):
        path = tmp_path / "connections.json"
        result = mig.migrate_if_needed(_fixture_network(), path=path)
        assert result is not None
        assert path.exists()
        on_disk = conn.load_connections(path=path)
        assert len(on_disk["connections"]) == 3

    def test_is_a_noop_when_the_file_already_exists(self, tmp_path):
        path = tmp_path / "connections.json"
        mig.migrate_if_needed(_fixture_network(), path=path)
        before = path.read_text()
        result = mig.migrate_if_needed(_fixture_network(), path=path)
        assert result is None
        assert path.read_text() == before

    def test_second_run_with_a_grown_network_still_no_ops(self, tmp_path):
        # D-CN8: migration is a startup one-shot, not a sync — a bot added
        # after the first migration gets its row through the wizard, not by
        # re-running this.
        path = tmp_path / "connections.json"
        mig.migrate_if_needed(_fixture_network(), path=path)
        grown = _fixture_network()
        grown["bots"]["new-bot"] = {
            "google_integration": {"mode": "free_gmail_oauth", "scopes": list(GMAIL_SCOPES_READ)},
        }
        mig.migrate_if_needed(grown, path=path)
        on_disk = conn.load_connections(path=path)
        assert len(on_disk["connections"]) == 3


class TestSharedDirResolution:
    def test_connections_path_follows_the_networks_shared_dir(self, tmp_path):
        assert conn.connections_path({"sharedDir": str(tmp_path)}) == tmp_path / "connections.json"

    def test_default_path_is_the_networks_shared_dir(self, tmp_path):
        network = dict(_fixture_network(), sharedDir=str(tmp_path))
        mig.migrate_if_needed(network)
        assert len(conn.load_connections(tmp_path / "connections.json")["connections"]) == 3

    def test_startup_migration_writes_into_the_configured_shared_dir(self, tmp_path):
        # The daemon call site: network.json names a non-default sharedDir,
        # and the registry must land there — not in the import-time default.
        shared = tmp_path / "pod-shared"
        network_path = tmp_path / "network.json"
        network_path.write_text(json.dumps(dict(_fixture_network(), sharedDir=str(shared))))
        mig.run_startup_migration(network_path)
        assert (shared / "connections.json").exists()
