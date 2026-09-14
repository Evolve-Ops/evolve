"""evolve_admin.skills.autocad_install — AutoCAD / Autodesk Platform Services skill.

AutoCAD is Autodesk's CAD platform. Programmatic access goes through
Autodesk Platform Services (APS, formerly Forge): listing DWG/RVT
files in a hub, fetching model metadata, kicking off Design Automation
workflows.

Auth shape (OAuth2, deferred to a follow-up)
---------------------------------------------
APS is an OAuth2-only platform — there is no static personal-API-key
shape like Runway or Linear. Three-legged Authorization Code is needed
for user-data access (hub contents, project files); two-legged Client
Credentials only works for app-owned resources. Either way requires:

  1. Pod operator registers an APS app at aps.autodesk.com → gets
     client_id + client_secret + configures redirect URI.
  2. The bot uses the OAuth flow per-user to get a 1-hour access token
     plus a refresh token, stored locally per bot.
  3. Token refresh loop runs in the background.

This is ~500 lines of installer + redirect-URI plumbing — out of scope
for this PR. We ship the catalog entry today so the skill is visible
and the access-panel copy is accurate; the install button points the
user at the APS app-registration docs and stays in a ``needs_app``
state until follow-up work wires the full OAuth dance.

Once the OAuth installer lands, this module will grow a verify_token /
refresh_token path and the resolve_status state machine expands to
mirror Notion's (missing | valid | revoked | invalid | unknown).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Skill identifier ──────────────────────────────────────────────────────────

AUTOCAD_SKILL_ID = "autocad"


# ── Plain-language access panel ───────────────────────────────────────────────

AUTOCAD_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": AUTOCAD_SKILL_ID,
    "skill_display_name": "AutoCAD (Autodesk Platform Services)",
    "summary": (
        "Lets this bot read AutoCAD drawings, Revit models, and other "
        "Autodesk files in your hub — list projects, fetch metadata, "
        "and surface drawing details. Useful for engineering and "
        "architecture workflows."
    ),
    "will": [
        "List projects and folders in your Autodesk hub",
        "Read drawing names, properties, and metadata",
        "Fetch model information for AutoCAD and Revit files",
    ],
    "wont": [
        "Modify or delete drawings without your explicit confirmation",
        "Share your Autodesk content with anyone outside this bot",
        "Access hubs belonging to other Autodesk accounts",
    ],
    "where_credentials_live": (
        "Your Autodesk sign-in is stored only on this bot's user account "
        "on your machine — never centralised, never sent off-pod. To "
        "revoke, open the Autodesk Account dashboard → Apps → revoke "
        "access for the evolve app."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the AutoCAD install flow.

    v1 ships with a single non-active state: ``needs_app``. Once the OAuth
    installer lands the state machine expands to mirror Notion's missing |
    valid | revoked | invalid | unknown shape.
    """

    bot_id: str
    status: str  # needs_app | unknown (v1); valid | revoked | invalid | missing (v2)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": AUTOCAD_SKILL_ID,
            "status": self.status,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    id: str
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


_APP_REGISTRATION_HINT = (
    "Autodesk requires a one-time app registration before bots can sign "
    "in. Open aps.autodesk.com → My Apps → Create App, choose the APIs "
    "you want the bot to use (Data Management is the usual starting "
    "point), and copy the Client ID + Client Secret. The follow-up "
    "install flow will wire the OAuth handshake; until that lands you "
    "can register the app now so it's ready."
)


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Return the steps remaining for *status*.

    v1 always returns the ``manual_setup`` + ``confirm`` pair — full OAuth
    handoff comes in a follow-up PR.  ``unknown`` short-circuits to an
    empty plan so the UI surfaces the error rather than steering the
    user to register an app that wouldn't help."""
    if status.status == "unknown":
        return []

    return [
        InstallStep(
            id="manual_setup",
            label=_APP_REGISTRATION_HINT,
            access_panel=dict(AUTOCAD_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm this bot can talk to Autodesk Platform Services",
            endpoint=f"/api/skills/install/{AUTOCAD_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(bot_id: str) -> InstallStatus:
    """v1 always returns ``needs_app``.

    A follow-up PR will swap in the real OAuth status resolver (check
    auth.profiles for an Autodesk OAuth profile + verify refresh-token
    validity against the APS API). For now: catalog discoverability +
    honest "registration needed" steer."""
    return InstallStatus(bot_id=bot_id, status="needs_app")


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": AUTOCAD_SKILL_ID,
    "display_name": AUTOCAD_ACCESS_PANEL["skill_display_name"],
    "summary": AUTOCAD_ACCESS_PANEL["summary"],
    "access_panel": dict(AUTOCAD_ACCESS_PANEL),
}
