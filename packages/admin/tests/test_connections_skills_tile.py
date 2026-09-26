"""test_connections_skills_tile.py — the Skills-tile health-text fixture.

D-CS7 / D-CN4: health names its subject and can say "unknown" — never a
check mark for a connection nobody has probed yet. Full visual wiring into
the Skills page (skills.js) is the ``connect-wizard-is-job-shaped`` chip's
job (D-CN7 — the design's own aspect boundary puts the wizard/tile in
``apps``, this chip is ``substrate``); this test pins the rendering
CONTRACT — ``connections.health_display`` and the ``/api/connections/<bot>``
JSON view — that surface will read from, using a fixture connection row
shaped exactly like a freshly-migrated one.
"""
from __future__ import annotations

from evolve_admin import connections as conn
from evolve_admin.web.connections_routes import _connection_view


def _freshly_migrated_row() -> dict:
    """Shaped exactly like connections_migration's output: unknown health,
    'not yet probed' reason — the Skills tile's most common real-world row
    the day after this chip ships."""
    return conn.new_connection(
        bot_id="lex",
        service="google",
        account_label="lex@example-corp.com",
        role="own",
        jobs=["read_person_calendar"],
        capabilities_=["calendar.read"],
        credential_ref="google_integration:lex",
        credential_kind="service_account_dwd",
        health=conn.new_health("unknown", reason="not yet probed"),
    )


class TestSkillsTileFixtureRendersHealthHonestly:
    def test_a_freshly_migrated_connection_renders_not_yet_verified(self):
        row = _freshly_migrated_row()
        assert conn.health_display(row["health"]) == "not yet verified"

    def test_the_connection_view_never_substitutes_a_check_mark_for_unknown(self):
        row = _freshly_migrated_row()
        view = _connection_view(row)
        assert view["health_display"] == "not yet verified"
        assert "✓" not in view["health_display"]

    def test_the_connection_view_carries_the_raw_state_too(self):
        # A future Skills-tile renderer needs both: the raw state (to pick
        # an icon/color) and the display text (to never re-derive its own
        # "connected" copy from the state).
        row = _freshly_migrated_row()
        view = _connection_view(row)
        assert view["health"]["state"] == "unknown"
        assert view["health_display"] == "not yet verified"

    def test_a_verified_connection_renders_verified_not_a_symbol(self):
        row = _freshly_migrated_row()
        row["health"] = conn.new_health("verified")
        view = _connection_view(row)
        assert view["health_display"] == "verified"
