"""evolve_admin.skills.gmail_install — Gmail-only skill install (V2.1-3).

Narrow-scope Gmail skill split off from the combined GOG skill.  Requesting
only Gmail scopes (no Calendar) means users who want email-reading capability
don't have to grant calendar access.

Trust-chain decisions (HIGH-STAKES — recorded for the reviewer):

1. **Scopes are strictly Gmail-readonly.**  The access panel promises the bot
   will not send email or modify the inbox.  Those promises are only true
   when the token carries only ``gmail.readonly``.  The write-capable
   ``gmail.send`` / ``gmail.modify`` variants live in the existing GWS
   wizard under "Advanced / write access" and are never requested here.

2. **Token stored per-skill, per-bot.**  The token lives at
   ``~/<bot_home>/.openclaw/auth-profiles.json`` under profile-id
   ``google_workspace:<bot_id>`` — same key as the GOG skill.  Gmail and
   Calendar use the same underlying Google OAuth profile because Google
   issues a single token per app-registration; the scope set determines
   capability.  The *skill-level* distinction is in which scopes are
   requested, not in separate token files.

3. **Backward compat: shared OAuth profile key.**  Gmail and Calendar both
   read/write the same ``google_workspace:<bot_id>`` profile.  Installing
   Gmail first and then Calendar will request a new consent that covers
   both scope sets; Google merges them into one token.  The profile is
   "active" for Gmail as soon as it contains ``gmail.readonly`` scope and
   for Calendar as soon as it contains ``calendar.readonly`` scope.

4. **No Composio.** Direct OpenClaw plugin path per
   ``project_external_dependency_vetting``.

5. **No centralized token store.** This module never sees access_token or
   refresh_token — those live only in ``/api/admin/onboard/google/callback``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Gmail-only skill.
GMAIL_SKILL_ID = "gmail"

#: The OpenClaw plugin entry backing Gmail (same as GOG — one Google plugin).
GMAIL_PLUGIN_NAME = "google"

#: The provider id under which Google OAuth tokens live in auth-profiles.json.
#: Shared with the GOG and Calendar skills — one Google OAuth app per pod.
GMAIL_PROVIDER_ID = "google_workspace"

#: Gmail-specific OAuth services (read-only by default).
#: This is narrower than GOG_DEFAULT_SERVICES which also includes calendar.
GMAIL_DEFAULT_SERVICES = ("gmail_readonly",)

#: Gmail scopes that satisfy this skill (used by resolve_status to check
#: whether an existing profile covers Gmail).  Any of these in the profile's
#: services list means Gmail is accessible.
GMAIL_SATISFYING_SERVICE_IDS = frozenset({
    "gmail_readonly",
    "gmail",         # write-capable variant; still satisfies read
    "gmail_modify",  # modify variant; still satisfies read
})


# ── Plain-language access panel ───────────────────────────────────────────────

GMAIL_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GMAIL_SKILL_ID,
    "skill_display_name": "Gmail",
    "summary": (
        "Lets this bot read your incoming email — subject, sender, and "
        "body — so it can surface what needs your attention. Used by "
        "Morning Briefing and any app that reasons over your inbox."
    ),
    "will": [
        "Read incoming emails (subject, sender, body) — so it can "
        "summarise what's worth your attention",
        "See your Google account email — so it knows which inbox to read",
    ],
    "wont": [
        "Send email on your behalf",
        "Delete or modify any emails",
        "Access Google Calendar, Drive, Docs, Sheets, or Slides",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "Tokens are stored only on this bot's user account on your machine — "
        "never centralised, never sent off-pod. You can revoke access at any "
        "time from this page or at myaccount.google.com/permissions."
    ),
    "default_services": list(GMAIL_DEFAULT_SERVICES),
}


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Gmail skill install flow.

    Mirrors the shape of ``gog_install.InstallStatus`` for UI consistency.

    Status values:
    * ``oauth_client_missing`` — pod has no GCP OAuth client; one-time setup needed.
    * ``plugin_disabled`` — OpenClaw ``google`` plugin not enabled on this bot.
    * ``oauth_pending`` — plugin enabled but no Gmail-capable OAuth profile present.
    * ``active`` — plugin enabled and profile covers Gmail access.
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
            "skill_id": GMAIL_SKILL_ID,
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
    """One step the UI must drive to complete a Gmail install."""

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
    """Build the ordered list of install steps for the Gmail skill."""
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
                endpoint=f"/api/skills/install/{GMAIL_SKILL_ID}/enable-plugin",
                payload={"bot_id": status.bot_id},
            )
        )

    plan.append(
        InstallStep(
            id="oauth",
            label="Sign in with Google to grant Gmail access",
            endpoint="/api/admin/onboard/google/begin",
            payload={
                "bot_id": status.bot_id,
                "services": list(GMAIL_DEFAULT_SERVICES),
            },
            access_panel=dict(GMAIL_ACCESS_PANEL),
        )
    )

    plan.append(
        InstallStep(
            id="confirm",
            label="Confirm the bot can read Gmail",
            endpoint=f"/api/skills/install/{GMAIL_SKILL_ID}/status",
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
    """Pure-Python Gmail status resolver.

    Same injectable-reader pattern as ``gog_install.resolve_status``.
    Checks that the OAuth profile covers at least one Gmail-capable scope.

    A profile is considered Gmail-active when:
    - ``profile["services"]`` contains at least one entry in
      ``GMAIL_SATISFYING_SERVICE_IDS`` (i.e. gmail_readonly, gmail, or
      gmail_modify), AND
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

    # Check whether the profile actually covers Gmail access
    gmail_covered = bool(
        has_profile
        and profile_status != "reauth_required"
        and GMAIL_SATISFYING_SERVICE_IDS.intersection(granted_services)
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

    if not gmail_covered:
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
    "id": GMAIL_SKILL_ID,
    "display_name": GMAIL_ACCESS_PANEL["skill_display_name"],
    "summary": GMAIL_ACCESS_PANEL["summary"],
    "plugin_name": GMAIL_PLUGIN_NAME,
    "provider_id": GMAIL_PROVIDER_ID,
    "default_services": list(GMAIL_DEFAULT_SERVICES),
    "access_panel": dict(GMAIL_ACCESS_PANEL),
}
