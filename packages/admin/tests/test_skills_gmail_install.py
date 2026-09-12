"""tests/test_skills_gmail_install.py — Gmail-only skill install flow (V2.1-3).

Pins the contract for the Gmail skill introduced in V2.1-3:

  - resolve_status routes by (oauth client configured, plugin enabled, profile
    present with gmail scope) into the five-state machine.
  - build_install_plan returns the right ordered steps for each state.
  - The plain-language access panel (Will/Won't copy) ships with the OAuth step.
  - Access panel promises are consistent with the narrow Gmail-only scopes.
  - Scope check: only gmail_readonly is requested at install time (no calendar).
  - Registry entry has correct ids and provider_id.

Key differences from gog_install tests:
  - resolve_status requires a Gmail-capable service id in the profile, not just
    any valid profile (a calendar-only profile is NOT active for Gmail).
  - GMAIL_DEFAULT_SERVICES does NOT include calendar_readonly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import gmail_install  # noqa: E402


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


def _gmail_profile(_bot_id):
    """Profile with gmail_readonly scope — satisfies Gmail."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["gmail_readonly"],
    }


def _calendar_only_profile(_bot_id):
    """Profile with only calendar scope — does NOT satisfy Gmail."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["calendar_readonly"],
    }


def _gog_profile(_bot_id):
    """Legacy GOG profile with both scopes — satisfies Gmail (has gmail_readonly)."""
    return {
        "status": "active",
        "google_account": "admin_bot@example.com",
        "services": ["gmail_readonly", "calendar_readonly"],
    }


def _reauth_profile(_bot_id):
    return {
        "status": "reauth_required",
        "google_account": "admin_bot@example.com",
        "services": ["gmail_readonly"],
    }


# ── resolve_status tests ──────────────────────────────────────────────────────


class TestResolveStatus:
    """Five-state machine for the Gmail skill."""

    def test_oauth_client_missing_short_circuits(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gmail_profile,
            read_oauth_client_configured=_no_client,
        )
        assert st.status == "oauth_client_missing"
        assert st.plugin_enabled is False
        assert st.has_oauth_profile is False

    def test_plugin_disabled(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_off,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "plugin_disabled"
        assert st.plugin_enabled is False

    def test_oauth_pending_when_no_profile(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.plugin_enabled is True
        assert st.has_oauth_profile is False

    def test_oauth_pending_when_reauth_required(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_reauth_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending"
        assert st.profile_status == "reauth_required"

    def test_oauth_pending_when_calendar_only_profile(self):
        """A calendar-only profile does NOT satisfy Gmail — must report oauth_pending."""
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_calendar_only_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "oauth_pending", (
            "Calendar-only profile should not satisfy Gmail skill"
        )

    def test_active_with_gmail_readonly(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gmail_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "active"
        assert st.google_account == "admin_bot@example.com"
        assert "gmail_readonly" in st.granted_services

    def test_active_with_legacy_gog_profile(self):
        """A legacy GOG profile (both scopes) satisfies Gmail."""
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gog_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "active"

    def test_unknown_when_plugin_read_fails(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_unknown,
            read_oauth_profile=_no_profile,
            read_oauth_client_configured=_ok_client,
        )
        assert st.status == "unknown"
        assert st.error is not None
        assert "plugin_inventory_unreadable" in st.error

    def test_to_dict_has_skill_id(self):
        st = gmail_install.resolve_status(
            "admin_bot",
            read_plugin_enabled=_plugin_on,
            read_oauth_profile=_gmail_profile,
            read_oauth_client_configured=_ok_client,
        )
        d = st.to_dict()
        assert d["skill_id"] == "gmail"


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
        return gmail_install.InstallStatus(**defaults)

    def test_active_returns_empty_plan(self):
        plan = gmail_install.build_install_plan(
            self._status(status="active", plugin_enabled=True, has_oauth_profile=True)
        )
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        plan = gmail_install.build_install_plan(self._status(status="unknown"))
        assert plan == []

    def test_oauth_client_missing_has_only_configure_step(self):
        plan = gmail_install.build_install_plan(self._status(status="oauth_client_missing"))
        assert [s.id for s in plan] == ["configure_oauth_client"]

    def test_plugin_disabled_walks_all_three_steps(self):
        plan = gmail_install.build_install_plan(self._status(status="plugin_disabled"))
        assert [s.id for s in plan] == ["enable_plugin", "oauth", "confirm"]

    def test_oauth_pending_skips_enable_plugin(self):
        plan = gmail_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        assert [s.id for s in plan] == ["oauth", "confirm"]

    def test_oauth_step_carries_gmail_access_panel(self):
        plan = gmail_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        oauth_step = next(s for s in plan if s.id == "oauth")
        assert oauth_step.access_panel is not None
        will = oauth_step.access_panel["will"]
        wont = oauth_step.access_panel["wont"]
        assert any("email" in w.lower() for w in will)
        assert any("send email" in w.lower() for w in wont)
        # Gmail panel must NOT mention Calendar as something it can do
        # (Calendar access is a separate skill now)
        assert all("calendar" not in w.lower() for w in will), (
            "Gmail access panel should not claim to read Calendar"
        )

    def test_oauth_step_requests_only_gmail_services(self):
        """Gmail install must only request gmail_readonly, not calendar_readonly."""
        plan = gmail_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        oauth_step = next(s for s in plan if s.id == "oauth")
        services = oauth_step.payload["services"]
        assert "gmail_readonly" in services
        assert "calendar_readonly" not in services, (
            "Gmail skill must not request calendar scope — Calendar is a separate skill"
        )

    def test_enable_plugin_endpoint_uses_gmail_skill_id(self):
        plan = gmail_install.build_install_plan(self._status(status="plugin_disabled"))
        ep = next(s for s in plan if s.id == "enable_plugin")
        assert "gmail" in ep.endpoint

    def test_confirm_endpoint_uses_gmail_skill_id(self):
        plan = gmail_install.build_install_plan(
            self._status(status="oauth_pending", plugin_enabled=True)
        )
        confirm = next(s for s in plan if s.id == "confirm")
        assert "gmail" in confirm.endpoint


# ── Access panel content tests ────────────────────────────────────────────────


class TestAccessPanelContent:
    def test_will_list_has_no_jargon(self):
        for line in gmail_install.GMAIL_ACCESS_PANEL["will"]:
            assert "scope" not in line.lower()
            assert "oauth" not in line.lower()

    def test_wont_list_includes_load_bearing_negatives(self):
        joined = " ".join(gmail_install.GMAIL_ACCESS_PANEL["wont"]).lower()
        assert "send email" in joined
        assert "delete" in joined or "modify" in joined

    def test_wont_mentions_no_calendar_access(self):
        """Gmail panel must say it doesn't access Calendar — key separation promise."""
        joined = " ".join(gmail_install.GMAIL_ACCESS_PANEL["wont"]).lower()
        assert "calendar" in joined, (
            "Gmail access panel must explicitly state it does not access Calendar"
        )

    def test_credentials_note_says_local_only(self):
        note = gmail_install.GMAIL_ACCESS_PANEL["where_credentials_live"].lower()
        assert "this bot" in note or "your machine" in note
        assert "centralised" in note or "centralized" in note or "never" in note


# ── Skill registry tests ──────────────────────────────────────────────────────


class TestSkillRegistry:
    def test_gmail_registry_entry_has_correct_ids(self):
        entry = gmail_install.SKILL_REGISTRY_ENTRY
        assert entry["id"] == "gmail"
        assert entry["plugin_name"] == "google"
        assert entry["provider_id"] == "google_workspace"

    def test_gmail_default_services_are_gmail_only(self):
        """The default services must be Gmail-only — no calendar."""
        assert "gmail_readonly" in gmail_install.GMAIL_DEFAULT_SERVICES
        assert "calendar_readonly" not in gmail_install.GMAIL_DEFAULT_SERVICES

    def test_satisfying_ids_include_write_capable_variants(self):
        """A bot with the write-capable gmail scope is still considered Gmail-active."""
        assert "gmail" in gmail_install.GMAIL_SATISFYING_SERVICE_IDS
        assert "gmail_modify" in gmail_install.GMAIL_SATISFYING_SERVICE_IDS
