"""tests/test_skills_calendar_install.py — Calendar-only skill install flow (V2.1-3).

Pins the contract for the Calendar skill introduced in V2.1-3:

  - resolve_status routes by (oauth client configured, plugin enabled, profile
    present with calendar scope) into the five-state machine.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel (Will/Won't copy) ships with the OAuth step.
  - Scope check: only calendar_readonly is requested at install time (no Gmail).
  - A Gmail-only profile does NOT satisfy Calendar.
  - A legacy GOG profile (both scopes) DOES satisfy Calendar.
  - Registry entry has correct ids and provider_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import calendar_install  # noqa: E402


# ── Reader stubs ──────────────────────────────────────────────────────────────


def _ok_client():
    return True


def _no_client():
    return False


def _plugin_on(_bot_id):
    return True


def _plugin_off(_bot_id):
    return False


def _plugin_unknown(_bot_id):
    return None


def _no_profile(_bot_id):
    return None


def _calendar_profile(_bot_id):
    """Profile with calendar_readonly scope — satisfies Calendar."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["calendar_readonly"],
    }


def _gmail_only_profile(_bot_id):
    """Profile with only Gmail scope — does NOT satisfy Calendar."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["gmail_readonly"],
    }


def _gog_profile(_bot_id):
    """Legacy GOG profile with both scopes — satisfies Calendar (has calendar_readonly)."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["gmail_readonly", "calendar_readonly"],
    }


def _reauth_profile(_bot_id):
    return {
        "status": "reauth_required",
        "google_account": "admin_bot@example.com",
        "services": ["calendar_readonly"],
    }


# ── resolve_status tests ──────────────────────────────────────────────────────


class TestResolveStatus:
    """Five-state machine for the Calendar skill."""

    def test_oauth_client_missing_short_circuits(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_calendar_profile,
            read_oauth_client_configured=_no_client,
        )
        assert st.status == "oauth_client_missing"
        assert st.plugin_enabled is False

    def test_plugin_disabled(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_off,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "plugin_disabled"
        assert st.plugin_enabled is False

    def test_oauth_pending_when_no_profile(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.plugin_enabled is True

    def test_oauth_pending_when_reauth_required(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_reauth_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.profile_status == "reauth_required"

    def test_oauth_pending_when_gmail_only_profile(self):
        """A Gmail-only profile does NOT satisfy Calendar — must report oauth_pending."""
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gmail_only_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending", (
            "Gmail-only profile should not satisfy Calendar skill"
        )

    def test_active_with_calendar_readonly(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_calendar_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "active"
        assert st.google_account == "admin_bot@example.com"
        assert "calendar_readonly" in st.granted_services

    def test_active_with_legacy_gog_profile(self):
        """A legacy GOG profile (both scopes) satisfies Calendar."""
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gog_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "active"

    def test_unknown_when_plugin_read_fails(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_unknown,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "unknown"
        assert st.error is not None
        assert "plugin_inventory_unreadable" in st.error

    def test_to_dict_has_skill_id(self):
        st = calendar_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_calendar_profile,
            read_oauth_client_configured=_ok_client,
        )
        d = st.to_dict()
        assert d["skill_id"] == "calendar"


# ── build_install_plan tests ──────────────────────────────────────────────────


class TestInstallPlan:
    def _status(self, **kw):
        defaults = dict(
            bot_id="admin_bot",
            status="plugin_disabled",
            plugin_enabled=False,
            has_oauth_profile=False,
        )
        defaults.update(kw)
        return calendar_install.InstallStatus(**defaults)

    def test_active_returns_empty_plan(self):
        plan = calendar_install.build_install_plan(
            self._status(status="active", plugin_enabled=True, has_oauth_profile=True)
        )
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        plan = calendar_install.build_install_plan(self._status(status="unknown"))
        assert plan == []

    def test_oauth_client_missing_has_only_configure_step(self):
        plan = calendar_install.build_install_plan(self._status(status="oauth_client_missing"))
        assert [s.id for s in plan] == ["configure_oauth_client"]

    def test_plugin_disabled_walks_all_three_steps(self):
        plan = calendar_install.build_install_plan(self._status(status="plugin_disabled"))
        assert [s.id for s in plan] == ["enable_plugin", "oauth", "confirm"]

    def test_oauth_pending_skips_enable_plugin(self):
        plan = calendar_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        assert [s.id for s in plan] == ["oauth", "confirm"]

    def test_oauth_step_carries_calendar_access_panel(self):
        plan = calendar_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert oauth_step.access_panel is not None
        will = oauth_step.access_panel["will"]
        wont = oauth_step.access_panel["wont"]
        assert any("calendar" in w.lower() for w in will)
        assert any("add" in w.lower() or "cancel" in w.lower() for w in wont)
        # Calendar panel must NOT claim to read email
        assert all("inbox" not in w.lower() for w in will), (
            "Calendar access panel should not claim to read Gmail inbox"
        )

    def test_oauth_step_requests_only_calendar_services(self):
        """Calendar install must only request calendar_readonly, not gmail_readonly."""
        plan = calendar_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        oauth_step = next(s for s in plan if s.id == "oauth")
        services = oauth_step.payload["services"]
        assert "calendar_readonly" in services
        assert "gmail_readonly" not in services, (
            "Calendar skill must not request gmail scope — Gmail is a separate skill"
        )

    def test_enable_plugin_endpoint_uses_calendar_skill_id(self):
        plan = calendar_install.build_install_plan(self._status(status="plugin_disabled"))
        ep = next(s for s in plan if s.id == "enable_plugin")
        assert "calendar" in ep.endpoint

    def test_confirm_endpoint_uses_calendar_skill_id(self):
        plan = calendar_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        confirm = next(s for s in plan if s.id == "confirm")
        assert "calendar" in confirm.endpoint


# ── Access panel content tests ────────────────────────────────────────────────


class TestAccessPanelContent:
    def test_will_list_has_no_jargon(self):
        for line in calendar_install.CALENDAR_ACCESS_PANEL["will"]:
            assert "scope" not in line.lower()
            assert "oauth" not in line.lower()

    def test_wont_list_includes_load_bearing_negatives(self):
        joined = " ".join(calendar_install.CALENDAR_ACCESS_PANEL["wont"]).lower()
        # Calendar-specific negatives
        assert "add" in joined or "cancel" in joined or "move" in joined

    def test_wont_mentions_no_gmail_access(self):
        """Calendar panel must say it doesn't access Gmail — key separation promise."""
        joined = " ".join(calendar_install.CALENDAR_ACCESS_PANEL["wont"]).lower()
        assert "gmail" in joined or "inbox" in joined or "email" in joined, (
            "Calendar access panel must explicitly state it does not access Gmail"
        )

    def test_credentials_note_says_local_only(self):
        note = calendar_install.CALENDAR_ACCESS_PANEL["where_credentials_live"].lower()
        assert "this bot" in note or "your machine" in note
        assert "centralised" in note or "centralized" in note or "never" in note


# ── Skill registry tests ──────────────────────────────────────────────────────


class TestSkillRegistry:
    def test_calendar_registry_entry_has_correct_ids(self):
        entry = calendar_install.SKILL_REGISTRY_ENTRY
        assert entry["id"] == "calendar"
        assert entry["plugin_name"] == "google"
        assert entry["provider_id"] == "google_workspace"

    def test_calendar_default_services_are_calendar_only(self):
        """The default services must be Calendar-only — no Gmail."""
        assert "calendar_readonly" in calendar_install.CALENDAR_DEFAULT_SERVICES
        assert "gmail_readonly" not in calendar_install.CALENDAR_DEFAULT_SERVICES

    def test_satisfying_ids_include_write_capable_variants(self):
        """A bot with the write-capable calendar scope is still considered Calendar-active."""
        assert "calendar" in calendar_install.CALENDAR_SATISFYING_SERVICE_IDS
        assert "calendar_events" in calendar_install.CALENDAR_SATISFYING_SERVICE_IDS
