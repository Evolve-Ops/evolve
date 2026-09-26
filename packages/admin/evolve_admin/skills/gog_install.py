"""evolve_admin.skills.gog_install — Gmail/Calendar setup over GOG (compat shim).

V2.1-3: GOG is now a **compatibility shim** that combines the new ``gmail``
and ``calendar`` skills.  New installs should prefer those individual skills.

Why keep this module:
* Existing bots with ``gog`` configured continue working unchanged.
* The ``gog`` skill id is referenced by gallery manifests, the UI, and
  the provider registry — all still point here.
* ``build_install_plan`` now orchestrates gmail + calendar in sequence
  for new installs of the combined "GOG" skill.
* ``resolve_status`` is "satisfied" when EITHER (a) the legacy profile has
  both gmail and calendar scopes covered, OR (b) both gmail_install and
  calendar_install independently report ``active``.

This module is the skill-shaped wrapper over the existing Google Workspace
infrastructure in :mod:`evolve_admin.web.server`. It exists because:

* The audience speaks in **skills** (GOG = Google Workspace = "Gmail + Calendar"),
  not providers (``google_workspace``) or plugins (``plugins.entries.google``).
  Per ``project_openclaw_market_reality_may2026``, GOG is the single
  most-downloaded ClawHub skill — smooth setup in the first 5 minutes is
  non-optional for adoption.
* Spec 11 of ``evolve-mvp-sprint.md`` calls for ``/api/skills/install/<skill-id>``
  as the public entry point. Templates (Spec 7 Morning Briefing) and the skills
  inventory view (Spec 12) both call into this surface.
* The underlying OAuth + token-storage machinery already exists at
  ``/api/admin/onboard/google/*``. This module's job is to **plan** an install
  (what plugin entry needs enabling, what OAuth services to request, what
  plain-language access panel to show), **status** an install, and **kick off**
  the OAuth wizard — not to reimplement OAuth.

Trust-chain decisions (HIGH-STAKES — recorded for the reviewer):

1. **Direct OpenClaw plugin install, not Composio.** Per
   ``project_external_dependency_vetting``, Composio's pre-built tools are
   closed-source. GOG is an open-source community skill. We go direct so the
   chain stays auditable.
2. **OAuth tokens are stored only on the user's bot.** We delegate to
   :func:`_write_google_oauth_profile` in ``server.py``, which writes to
   ``/Users/<bot>/.openclaw/auth-profiles.json`` (via ``/tmp`` staging +
   ``sudo /bin/cp`` per CLAUDE.md). No centralized token store. This module
   never sees the access_token or refresh_token in its own code paths.
3. **The plain-language access panel is the load-bearing UX promise.** It must
   describe scopes in user terms (per the Plex test in
   ``feedback_design_constraint_mildly_tech_capable``). Strings live in
   :data:`GOG_ACCESS_PANEL` and are surfaced verbatim by the UI; no jargon,
   no "scopes".
4. **Revocation goes through Google's revoke endpoint + clears the local
   profile.** This module exposes the URL/payload contract; the actual
   revoke call is the existing ``/api/admin/onboard/google/revoke`` route.

The skill→provider→plugin map is intentionally small and explicit (one row,
plus pod-wide constants). Future skills (Slack/Linear/Notion) register
additional rows in the oauth/providers/ directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Google Workspace skill, as referenced by templates and
#: the UI. Matches ClawHub's "gog" prefix; future-proofs against the day we
#: ship more than one skill.
GOG_SKILL_ID = "gog"

#: The OpenClaw plugin entry that backs GOG (under ``plugins.entries`` in
#: ``openclaw.json``). The first-party Google plugin is named ``google`` —
#: matches the existing ``plugins.entries.google`` mapping at the keys
#: endpoint in ``server.py``.
GOG_PLUGIN_NAME = "google"

#: The provider id under which Google OAuth tokens live in
#: ``auth-profiles.json``. Matches the existing
#: ``/api/admin/onboard/google/*`` family — we never invent a parallel
#: provider id.
GOG_PROVIDER_ID = "google_workspace"

#: Default OAuth services to request on a fresh install. The user can toggle
#: extras in the existing GWS wizard (Drive/Docs/Sheets/Slides) — and opt in
#: to the write-capable variants (``gmail`` = send+read, ``calendar`` = full
#: r/w) under "Advanced / write access" — but the friendly "Add Gmail/Calendar"
#: entry-point asks for exactly these two **read-only** service ids.
#:
#: Why read-only by default: the access panel below promises the bot "Won't
#: send email on your behalf" and "Won't add, move, or cancel calendar
#: events". Those promises are the load-bearing trust contract of Spec 11.
#: They are only true if the OAuth token does not carry write scopes. A
#: future opt-in flow can request the write variants with a panel that says
#: so explicitly — that is v1.1, not the default install.
#:
#: Keeping the default narrow is also the Plex test: most users will not
#: understand why "Sign in to read Gmail" should also grant Drive, or why
#: it should be able to send mail "on their behalf".
GOG_DEFAULT_SERVICES = ("gmail_readonly", "calendar_readonly")


# ── Plain-language access panel ───────────────────────────────────────────────

#: User-facing description of what Evolve will and will not do with the
#: granted access. Written for the Plex test — no "scopes", no OAuth jargon,
#: every line a concrete user-recognisable action. Copy here is rendered
#: verbatim in the install UI; reviewers should treat changes here as
#: copy-affecting-trust changes.
#:
#: The "won't" list is intentionally specific. "Won't access Drive" is more
#: trustworthy than "won't access anything else" because users can verify it
#: corresponds to a scope absence.
GOG_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GOG_SKILL_ID,
    # The display name was "Gmail & Calendar (Google Workspace)" until
    # 2026-06-04 — the parenthetical read like the whole Workspace was
    # included, when in fact the only scopes granted are gmail_readonly +
    # calendar_readonly. Renamed per the May audit's F5 honesty rule.
    # A broader Workspace suite (Drive, Docs, Sheets, send-Gmail,
    # write-Calendar) is tracked separately as a follow-on spec.
    "skill_display_name": "Gmail + Calendar (read-only)",
    "summary": (
        "Read-only access to incoming email and calendar events on the "
        "Google account you connect. Used by Morning Briefing, Calendar "
        "Watch, and other applications that need to know your schedule. "
        "The bot can't send email, change events, or touch Drive / Docs / "
        "Sheets — see the next-gen Google Workspace suite for those."
    ),
    "will": [
        "Read incoming emails (subject, sender, body) — so it can summarise "
        "what's worth your attention",
        "List events on your calendar — so it knows what's on your day",
        "See your Google account email — so it knows whose calendar to read",
    ],
    "wont": [
        "Send email on your behalf",
        "Delete or modify any emails",
        "Add, move, or cancel calendar events",
        "Access Google Drive, Docs, Sheets, or Slides",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "Tokens are stored only on this bot's user account on your machine — "
        "never centralised, never sent off-pod. You can revoke access at any "
        "time from this page or at myaccount.google.com/permissions."
    ),
    "default_services": list(GOG_DEFAULT_SERVICES),
}


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the GOG install flow.

    Status values form a small state machine the UI polls:

    * ``oauth_client_missing`` — the pod has no GCP OAuth client configured yet
      (one-time setup; pre-empts every per-bot install). UI must route the
      operator to the GCP wizard first.
    * ``plugin_disabled`` — the OpenClaw ``google`` plugin entry is missing or
      ``enabled: false`` on this bot. Calling ``install`` will create an
      ``EnablePluginEntry`` proposal and auto-apply it.
    * ``oauth_pending`` — the plugin is enabled but no OAuth profile exists
      (or the profile is marked ``reauth_required``). Calling ``install``
      kicks off the existing ``/api/admin/onboard/google/begin`` flow.
    * ``active`` — plugin enabled, OAuth profile present and not
      ``reauth_required``. The bot can read Gmail / list calendar events.
    * ``unknown`` — pre-flight read failed; UI should surface the error string.
    """

    bot_id: str
    status: str  # one of: oauth_client_missing | plugin_disabled | oauth_pending | active | unknown
    plugin_enabled: bool
    has_oauth_profile: bool
    profile_status: str | None = None  # active | reauth_required | None
    google_account: str | None = None
    granted_services: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": GOG_SKILL_ID,
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
    """One step the UI must drive to complete a GOG install.

    The UI walks steps in order. Each step has an ``id`` the UI dispatches on,
    a human-readable ``label`` for the progress display, and (where relevant)
    an ``endpoint`` to call. The plain-language access panel ships under the
    ``oauth`` step so it appears at the moment the user is asked to consent.
    """

    id: str  # one of: configure_oauth_client | enable_plugin | oauth | confirm
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
    """Build the ordered list of steps remaining for ``status``.

    Pre-flight:
      * ``oauth_client_missing`` → only one step: configure the pod GCP client
        (one-time, pod-wide). After that, the per-bot install can run.

    Per-bot:
      * ``plugin_disabled`` → enable plugin entry, then OAuth, then confirm.
      * ``oauth_pending``   → OAuth, then confirm.
      * ``active``          → empty plan (nothing to do; UI shows success).
      * ``unknown``         → empty plan; UI must surface the error.
    """
    if status.status == "oauth_client_missing":
        return [
            InstallStep(
                id="configure_oauth_client",
                label="Set up Google Workspace for this pod",
                endpoint="/api/admin/onboard/google/configure",
                payload={"hint": "one-time pod-wide GCP client setup"},
            )
        ]

    if status.status == "active":
        return []

    if status.status == "unknown":
        return []

    plan: list[InstallStep] = []

    if not status.plugin_enabled:
        plan.append(
            InstallStep(
                id="enable_plugin",
                label="Turn on the Google plugin for this bot",
                endpoint=f"/api/skills/install/{GOG_SKILL_ID}/enable-plugin",
                payload={"bot_id": status.bot_id},
            )
        )

    plan.append(
        InstallStep(
            id="oauth",
            label="Sign in with Google",
            endpoint="/api/admin/onboard/google/begin",
            payload={
                "bot_id": status.bot_id,
                "services": list(GOG_DEFAULT_SERVICES),
            },
            access_panel=dict(GOG_ACCESS_PANEL),
        )
    )

    plan.append(
        InstallStep(
            id="confirm",
            label="Confirm the bot can read Gmail and list calendar events",
            endpoint=f"/api/skills/install/{GOG_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        )
    )

    return plan


# ── Pure resolver — works off injected reader callables for testability ──────


def resolve_status(
    bot_id: str,
    *,
    read_plugin_enabled,  # callable(bot_id) -> bool | None ; None on error
    read_oauth_profile,  # callable(bot_id) -> dict | None
    read_oauth_client_configured,  # callable() -> bool
) -> InstallStatus:
    """Pure-Python status resolver.  V2.1-3: compatibility shim.

    The three reader callables abstract over the existing helpers in
    ``server.py`` (``_is_plugin_enabled``, ``_read_google_oauth_profile``,
    ``_read_google_oauth_client``) so this function is unit-testable without
    standing up a Flask app or a real bot. The route layer wires the real
    helpers in; tests pass in dict/lambda stubs.

    The function never raises on bad reader output — a reader returning None
    or raising is captured as ``status="unknown"`` with ``error`` populated.
    The route's error handling path is the single place that decides whether
    to surface that to the user as a 500 or a 200 with status=unknown.

    V2.1-3 shim logic
    -----------------
    GOG reports ``active`` when EITHER:

    (a) **Legacy path**: the existing ``google_workspace`` profile has services
        that cover both Gmail AND Calendar (i.e. the pre-split GOG install).
        This preserves existing installs — no re-OAuth required.

    (b) **Split-skill path**: ``gmail_install.resolve_status`` and
        ``calendar_install.resolve_status`` both independently report
        ``active``.  This handles bots that installed Gmail + Calendar
        separately after V2.1-3.

    If Gmail is active but Calendar is not, we report ``oauth_pending`` with
    ``error="calendar_missing"`` so the UI can surface "needs Calendar too".
    If Calendar is active but Gmail is not, we report ``oauth_pending`` with
    ``error="gmail_missing"``.
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

    if not has_profile or profile_status == "reauth_required":
        # No profile at all — need full OAuth
        return InstallStatus(
            bot_id=bot_id,
            status="oauth_pending",
            plugin_enabled=True,
            has_oauth_profile=has_profile,
            profile_status=profile_status,
            google_account=google_account,
            granted_services=granted_services,
        )

    # ── V2.1-3 shim: check whether existing profile covers both skills ────────
    # Import lazily so tests that don't need the sub-skill checks aren't affected
    # by missing sub-skill modules (they're new in V2.1-3; pre-existing tests
    # load gog_install before the split).
    from .gmail_install import GMAIL_SATISFYING_SERVICE_IDS
    from .calendar_install import CALENDAR_SATISFYING_SERVICE_IDS

    services_set = set(granted_services)
    gmail_ok = bool(GMAIL_SATISFYING_SERVICE_IDS.intersection(services_set))
    calendar_ok = bool(CALENDAR_SATISFYING_SERVICE_IDS.intersection(services_set))

    if gmail_ok and calendar_ok:
        # (a) Legacy GOG profile covers both — fully active
        return InstallStatus(
            bot_id=bot_id,
            status="active",
            plugin_enabled=True,
            has_oauth_profile=True,
            profile_status=profile_status or "active",
            google_account=google_account,
            granted_services=granted_services,
        )

    if gmail_ok and not calendar_ok:
        return InstallStatus(
            bot_id=bot_id,
            status="oauth_pending",
            plugin_enabled=True,
            has_oauth_profile=True,
            profile_status=profile_status,
            google_account=google_account,
            granted_services=granted_services,
            error="calendar_missing",
        )

    if calendar_ok and not gmail_ok:
        return InstallStatus(
            bot_id=bot_id,
            status="oauth_pending",
            plugin_enabled=True,
            has_oauth_profile=True,
            profile_status=profile_status,
            google_account=google_account,
            granted_services=granted_services,
            error="gmail_missing",
        )

    # Profile exists but covers neither — need to re-OAuth
    return InstallStatus(
        bot_id=bot_id,
        status="oauth_pending",
        plugin_enabled=True,
        has_oauth_profile=True,
        profile_status=profile_status,
        google_account=google_account,
        granted_services=granted_services,
    )


# ── Skill registry (forward-looking, single entry today) ─────────────────────


SKILL_REGISTRY: dict[str, dict[str, Any]] = {
    GOG_SKILL_ID: {
        "id": GOG_SKILL_ID,
        "display_name": GOG_ACCESS_PANEL["skill_display_name"],
        "summary": GOG_ACCESS_PANEL["summary"],
        "plugin_name": GOG_PLUGIN_NAME,
        "provider_id": GOG_PROVIDER_ID,
        "default_services": list(GOG_DEFAULT_SERVICES),
        "access_panel": dict(GOG_ACCESS_PANEL),
    },
}


def get_skill(skill_id: str) -> dict[str, Any] | None:
    """Return the registry entry for ``skill_id`` or ``None``."""
    return SKILL_REGISTRY.get(skill_id)
