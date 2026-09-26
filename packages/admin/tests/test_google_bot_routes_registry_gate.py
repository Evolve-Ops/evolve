"""test_google_bot_routes_registry_gate.py — D-CN8 registry-first eligibility.

``_is_google_eligible`` (web/google_bot_routes.py) must consult the
Connection registry — resolved from the network's ``sharedDir`` — first and
fall back to the legacy ``google_integration``-mode check when no row exists,
or when the only row is an empty scope-shaped migration row. A migrated row
may only narrow eligibility, never zero out a bot the legacy path serves.
"""
from __future__ import annotations

import pytest

from evolve_admin import connections as conn
from evolve_admin import connections_migration as mig
from evolve_admin import google_service
from evolve_admin.web import google_bot_routes
from evolve_admin.web.google_bot_routes import _is_google_eligible


def _network(tmp_path, bot_cfg=None):
    return {
        "sharedDir": str(tmp_path),
        "bots": {"lex": bot_cfg if bot_cfg is not None else {"role": "member"}},
    }


def _configured_network(tmp_path, mode="service_account_dwd", scopes=None):
    return _network(tmp_path, {
        "role": "member",
        "google_integration": {
            "mode": mode,
            "scopes": ["https://www.googleapis.com/auth/calendar.readonly"] if scopes is None else scopes,
        },
    })


def _save_row(tmp_path, **kw):
    row = conn.new_connection(
        bot_id="lex", service="google", account_label="lex@example.com", role="own",
        credential_ref="google_integration:lex", credential_kind="service_account_dwd", **kw,
    )
    conn.save_connections({"connections": [row]}, path=tmp_path / "connections.json")


class TestRegistryFirstEligibility:
    def test_no_registry_row_falls_back_to_legacy_check(self, tmp_path):
        assert _is_google_eligible("lex", _configured_network(tmp_path)) is True

    def test_no_registry_row_and_unconfigured_legacy_is_false(self, tmp_path):
        assert _is_google_eligible("lex", _network(tmp_path)) is False

    def test_registry_row_with_capabilities_wins_even_if_legacy_looks_unconfigured(self, tmp_path):
        _save_row(tmp_path, jobs=["read_person_calendar"], capabilities_=["calendar.read"])
        # Legacy block absent entirely — the registry row is the only truth.
        assert _is_google_eligible("lex", _network(tmp_path)) is True

    def test_registry_is_read_from_the_networks_shared_dir(self, tmp_path):
        # A row in a DIFFERENT dir is not this pod's registry: the gate must
        # not see it and falls back to legacy (unconfigured → False).
        other = tmp_path / "elsewhere"
        other.mkdir()
        _save_row(other, jobs=["read_person_calendar"], capabilities_=["calendar.read"])
        assert _is_google_eligible("lex", _network(tmp_path)) is False
        assert _is_google_eligible("lex", _network(other)) is True

    def test_empty_scope_shaped_migration_row_defers_to_the_legacy_check(self, tmp_path):
        """Proves a migrated row that derived no verbs ("the literal match
        could not tell") never disables a bot the legacy path serves: the
        registry says None and ``is_google_configured`` decides — True for a
        configured bot, False for an unconfigured one."""
        _save_row(tmp_path, jobs=[], capabilities_=[],
                  health=conn.new_health("unknown", reason=conn.SCOPE_SHAPED_REASON))
        assert conn.is_configured_via_registry(
            "lex", "google", path=tmp_path / "connections.json") is None
        assert _is_google_eligible("lex", _configured_network(tmp_path)) is True
        assert _is_google_eligible("lex", _network(tmp_path)) is False

    def test_empty_row_without_the_scope_shaped_reason_stays_ineligible(self, tmp_path):
        # Only the migration's "could not tell" row defers; an empty row that
        # says anything else (e.g. a probe later settled it) still narrows.
        _save_row(tmp_path, jobs=[], capabilities_=[],
                  health=conn.new_health("failed", reason="token revoked"))
        assert _is_google_eligible("lex", _configured_network(tmp_path)) is False


_MAIL_ALL = "https://mail.google.com/"
_DRIVE = "https://www.googleapis.com/auth/drive"


class TestBroadScopeBotsSurviveTheMigration:
    """The three grants the literal scope match under-reports. Each works on
    the legacy path today; after ``migrate_if_needed`` each must still be
    eligible, via the legacy check the scope-shaped row defers to."""

    @pytest.mark.parametrize(("mode", "scopes"), [
        ("service_account_dwd", [_MAIL_ALL]),
        ("free_gmail_oauth", [_MAIL_ALL]),
        ("service_account_dwd", [_DRIVE]),
        ("service_account_dwd", []),
    ], ids=["mail-only-dwd", "mail-only-oauth", "drive-only", "dwd-empty-scopes"])
    def test_broad_scope_bot_is_eligible_after_migration(self, tmp_path, monkeypatch, mode, scopes):
        network = _configured_network(tmp_path, mode=mode, scopes=scopes)
        assert mig.migrate_if_needed(network) is not None
        assert (tmp_path / "connections.json").exists()
        row = conn.for_bot("lex", path=conn.connections_path(network))[0]
        assert row["capabilities"] == []
        assert row["health"]["state"] == "unknown"
        assert row["health"]["reason"] == conn.SCOPE_SHAPED_REASON

        calls: list[str] = []
        real = google_service.is_google_configured

        def _counting(bot_id, net):
            calls.append(bot_id)
            return real(bot_id, net)

        monkeypatch.setattr(google_bot_routes.google_service, "is_google_configured", _counting)
        assert _is_google_eligible("lex", network) is True
        assert calls == ["lex"], "the legacy check must decide for a scope-shaped empty row"
