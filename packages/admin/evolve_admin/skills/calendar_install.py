"""evolve_admin.skills.calendar_install — Calendar-only skill install (V2.1-3).

Narrow-scope Calendar skill split off from the combined GOG skill.  Requesting
only Calendar scopes means users who want schedule-reading capability don't
have to grant Gmail access.

This is especially important for the Note-taker app (V2.1-5) and any bot that
needs meeting awareness but not email access.

Trust-chain decisions (HIGH-STAKES — recorded for the reviewer):

1. **Scopes are strictly Calendar-readonly by default.**  The access panel
   promises the bot will not add, move, or cancel events.  Only
   ``calendar.readonly`` is requested at install time.  The write-capable
   ``calendar`` and ``calendar.events`` variants require an explicit opt-in
   through the advanced wizard; they are never requested here.

2. **Token shared with Gmail/GOG via the same Google OAuth profile.**  Google
   issues one token per OAuth application registration; the scope set encodes
   capabilities.  Calendar and Gmail use the same ``google_workspace:<bot_id>``
   profile key.  A bot that installs Calendar after Gmail will get a new
   OAuth consent that merges both scope sets.

3. **No Composio.** Direct OpenClaw plugin path per
   ``project_external_dependency_vetting``.

4. **No centralized token store.** This module never sees access_token or
   refresh_token — those live only in ``/api/admin/onboard/google/callback``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Calendar-only skill.
CALENDAR_SKILL_ID = "calendar"

#: The OpenClaw plugin entry backing Calendar (same as GOG — one Google plugin).
CALENDAR_PLUGIN_NAME = "google"

#: The provider id under which Google OAuth tokens live in auth-profiles.json.
#: Shared with the GOG and Gmail skills — one Google OAuth app per pod.
CALENDAR_PROVIDER_ID = "google_workspace"

#: Calendar-specific OAuth services (read-only by default).
CALENDAR_DEFAULT_SERVICES = ("calendar_readonly",)

#: Calendar scopes that satisfy this skill (used by resolve_status to check
#: whether an existing profile covers Calendar).  Any of these in the profile's
#: services list means Calendar is accessible.
CALENDAR_SATISFYING_SERVICE_IDS = frozenset({
    "calendar_readonly",
    "calendar",         # full r/w variant; still satisfies read
    "calendar_events",  # events-scope variant; still satisfies read
})


# ── Plain-language access panel ───────────────────────────────────────────────

CALENDAR_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": CALENDAR_SKILL_ID,
    "skill_display_name": "Google Calendar",
    "summary": (
        "Lets this bot see what's on your calendar — upcoming events, "
        "times, and attendees. Used by Morning Briefing, the Note-taker, "
        "and any app that needs to know your schedule."
    ),
    "will": [
        "List events on your calendar — so it knows what's on your day",
        "See event details (time, title, attendees) — so it can prepare "
        "context for your meetings",
        "See your Google account email — so it knows whose calendar to read",
    ],
    "wont": [
        "Add, move, or cancel calendar events",
        "Access your Gmail inbox",
        "Access Google Drive, Docs, Sheets, or Slides",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "Tokens are stored only on this bot's user account on your machine — "
        "never centralised, never sent off-pod. You can revoke access at any "
        "time from this page or at myaccount.google.com/permissions."
    ),
    "default_services": list(CALENDAR_DEFAULT_SERVICES),
}


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Calendar skill install flow.

    Mirrors the shape of ``gog_install.InstallStatus`` for UI consistency.

    Status values:
    * ``oauth_client_missing`` — pod has no GCP OAuth client; one-time setup needed.
    * ``plugin_disabled`` — OpenClaw ``google`` plugin not enabled on this bot.
    * ``oauth_pending`` — plugin enabled but no Calendar-capable OAuth profile present.
    * ``active`` — plugin enabled and profile covers Calendar access.
    * ``unknown`` — pre-flight read failed; error string populated.
    """

    bot_id: str
    status: str  # oauth_client_missing | plugin_disabled | oauth_pending | active | unknown
    plugin_enabled: bool
    has_oauth_profile: bool
    profile_status: str | None = None
    google_account: str | None = None
    granted_services: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": CALENDAR_SKILL_ID,
            "status": self.status,
            "plugin_enabled": self.plugin_enabled,
            "has_oauth_profile": self.has_oauth_profile,
            "profile_status": self.profile_status,
            "google_account": self.google_account,
            "granted_services": list(self.granted_services),
            "error": self.error,
        }


@dataclass
class InstallStep:
    """One step the UI must drive to complete a Calendar install."""

    id: str  # configure_oauth_client | enable_plugin | oauth | confirm
    label: str
    endpoint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    access_panel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "access_panel": self.access_panel,
        }


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Build the ordered list of install steps for the Calendar skill."""
    if status.status == "oauth_client_missing":
        return [
            InstallStep(
                id="configure_oauth_client",
                label="Set up Google Workspace for this pod",
                endpoint="/api/admin/onboard/google/configure",
                payload={"hint": "one-time pod-wide GCP client setup"},
            )
        ]

    if status.status in ("active", "unknown"):
        return []

    plan: list[InstallStep] = []

    if not status.plugin_enabled:
        plan.append(
            InstallStep(
                id="enable_plugin",
                label="Turn on the Google plugin for this bot",
                endpoint=f"/api/skills/install/{CALENDAR_SKILL_ID}/enable-plugin",
                payload={"bot_id": status.bot_id},
            )
        )

    plan.append(
        InstallStep(
            id="oauth",
            label="Sign in with Google to grant Calendar access",
            endpoint="/api/admin/onboard/google/begin",
            payload={
                "bot_id": status.bot_id,
                "services": list(CALENDAR_DEFAULT_SERVICES),
            },
            access_panel=dict(CALENDAR_ACCESS_PANEL),
        )
    )

    plan.append(
        InstallStep(
            id="confirm",
            label="Confirm the bot can list calendar events",
            endpoint=f"/api/skills/install/{CALENDAR_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        )
    )

    return plan


# ── Pure resolver ─────────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_plugin_enabled,  # callable(bot_id) -> bool | None
    read_oauth_profile,   # callable(bot_id) -> dict | None
    read_oauth_client_configured,  # callable() -> bool
) -> InstallStatus:
    """Pure-Python Calendar status resolver.

    Same injectable-reader pattern as ``gog_install.resolve_status``.
    Checks that the OAuth profile covers at least one Calendar-capable scope.

    A profile is considered Calendar-active when:
    - ``profile["services"]`` contains at least one entry in
      ``CALENDAR_SATISFYING_SERVICE_IDS`` (calendar_readonly, calendar, or
      calendar_events), AND
    - ``profile["status"]`` is not ``"reauth_required"``.
    """
    try:
        client_ok = bool(read_oauth_client_configured())
    except Exception as exc:  # pragma: no cover — defensive
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            plugin_enabled=False,
            has_oauth_profile=False,
            error=f"oauth_client_check_failed: {exc.__class__.__name__}: {exc}",
        )

    if not client_ok:
        return InstallStatus(
            bot_id=bot_id,
            status="oauth_client_missing",
            plugin_enabled=False,
            has_oauth_profile=False,
        )

    try:
        plugin_enabled = read_plugin_enabled(bot_id)
    except Exception as exc:  # pragma: no cover — defensive
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            plugin_enabled=False,
            has_oauth_profile=False,
            error=f"plugin_read_failed: {exc.__class__.__name__}: {exc}",
        )

    if plugin_enabled is None:
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            plugin_enabled=False,
            has_oauth_profile=False,
            error="plugin_inventory_unreadable",
        )

    try:
        profile = read_oauth_profile(bot_id)
    except Exception as exc:  # pragma: no cover — defensive
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            plugin_enabled=bool(plugin_enabled),
            has_oauth_profile=False,
            error=f"oauth_profile_read_failed: {exc.__class__.__name__}: {exc}",
        )

    has_profile = bool(profile)
    profile_status = (profile or {}).get("status") if has_profile else None
    google_account = (profile or {}).get("google_account") if has_profile else None
    granted_services = list((profile or {}).get("services") or []) if has_profile else []

    # Check whether the profile actually covers Calendar access
    calendar_covered = bool(
        has_profile
        and profile_status != "reauth_required"
        and CALENDAR_SATISFYING_SERVICE_IDS.intersection(granted_services)
    )

    if not plugin_enabled:
        return InstallStatus(
            bot_id=bot_id,
            status="plugin_disabled",
            plugin_enabled=False,
            has_oauth_profile=has_profile,
            profile_status=profile_status,
            google_account=google_account,
            granted_services=granted_services,
        )

    if not calendar_covered:
        return InstallStatus(
            bot_id=bot_id,
            status="oauth_pending",
            plugin_enabled=True,
            has_oauth_profile=has_profile,
            profile_status=profile_status,
            google_account=google_account,
            granted_services=granted_services,
        )

    return InstallStatus(
        bot_id=bot_id,
        status="active",
        plugin_enabled=True,
        has_oauth_profile=True,
        profile_status=profile_status or "active",
        google_account=google_account,
        granted_services=granted_services,
    )


# ── Skill registry entry ──────────────────────────────────────────────────────

SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": CALENDAR_SKILL_ID,
    "display_name": CALENDAR_ACCESS_PANEL["skill_display_name"],
    "summary": CALENDAR_ACCESS_PANEL["summary"],
    "plugin_name": CALENDAR_PLUGIN_NAME,
    "provider_id": CALENDAR_PROVIDER_ID,
    "default_services": list(CALENDAR_DEFAULT_SERVICES),
    "access_panel": dict(CALENDAR_ACCESS_PANEL),
}
