"""evolve_admin.skills.google_install — the unified Google skill.

Replaces the catalog presentation of three overlapping skills (gog,
google_workspace_read, google_workspace_write) with ONE row whose
wizard negotiates per-capability scope at install time. Same underlying
infrastructure (OAuth profile shape, MCP server, token shim, keystore
slots), different presentation layer.

Rationale (PR #2154 review feedback):

  The split-skill design (Read + Write as two catalog rows) was an IA
  mistake. Users on the Plex-test track shouldn't have to decide what
  scope to grant before they've even started the install — that's the
  wizard's job. Diana's per-bot compartmentalisation is achieved by
  picking different capability sets per bot, in the wizard, not by
  picking different skills.

  Catalog represents CAPABILITIES the bot can have. The wizard
  negotiates exactly which capabilities THIS bot gets. iOS app-permission
  model: one app entry, runtime scope grant.

This module:

  * Owns the capability catalog — see :data:`GOOGLE_CAPABILITIES`.
  * Provides the sensible default capability set (Read Gmail + Read
    Calendar — matches the legacy ``gog`` skill, low-friction Plex-test
    path).
  * Builds the install plan (capability picker → OAuth → complete).
  * Wraps the existing ``_gws_complete_install_impl`` route helper with
    a per-install capability set rather than the WRITE/READ baked-in
    extra_args.
  * Detects legacy ``gog``-style installs in the resolver — a bot with
    ``gmail.readonly`` + ``calendar.readonly`` shows as installed under
    "Google" without re-OAuth needed.

Trust-chain decisions:

1. **Same OAuth profile, same MCP server.** Reuses
   ``google_workspace:<bot_id>`` profile and ``mcp.servers.google_workspace``
   entry from the underlying infrastructure. Switching capabilities
   mid-life writes a new MCP entry (replaces in place); OAuth profile
   accumulates scopes across re-consent.

2. **Capability set lives in the granted OAuth scopes — no sidecar
   state.** A bot's "current capabilities" is derived from
   ``profile.scopes``. No separate file tracks "what the operator
   picked." This avoids the consistency-drift problem (profile says X,
   sidecar says Y).

3. **Sensible default = gmail_read + calendar_read.** Matches the legacy
   ``gog`` defaults (Plex-test continuity); advanced users uncheck or
   add capabilities deliberately.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

# Underlying shared infrastructure — keystore-slot helpers, preflight
# check, CompletionResult shape, MCP server id, catalog id. The Write
# module landed first so it owns the shared constants and this unified
# module re-exports them. (The original ``_read`` sibling that also
# re-exported was deleted as vestigial in a follow-up cleanup PR.)
from . import google_workspace_write_install as _shared

log = logging.getLogger(__name__)


# ── Skill identifier ────────────────────────────────────────────────────────


#: Canonical skill id surfaced in the catalog.
GOOGLE_SKILL_ID = "google"

#: Re-exported from the shared module.
GOOGLE_MCP_SERVER_ID = _shared.GOOGLE_WORKSPACE_MCP_SERVER_ID
GOOGLE_CATALOG_ID = _shared.GOOGLE_WORKSPACE_CATALOG_ID

#: Re-exported keystore-slot helpers and preflight (shared with the
#: hidden _read / _write modules — same per-bot slots).
keystore_slot_client_id_for = _shared.keystore_slot_client_id_for
keystore_slot_client_secret_for = _shared.keystore_slot_client_secret_for
keystore_slot_credentials_dir_for = _shared.keystore_slot_credentials_dir_for
preflight_check = _shared.preflight_check
CompletionResult = _shared.CompletionResult


# ── Capability catalog ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capability:
    """One capability the operator can toggle in the wizard.

    Each capability is a (service_id, scope, MCP-permission) triple. The
    service_id flows into ``/api/admin/onboard/google/begin``; the scope
    is checked by :func:`derive_granted_capabilities` against the OAuth
    profile; the MCP permission is passed as ``--permissions`` to the
    workspace-mcp server.

    Attributes:
      id:                Stable identifier used in wizard payloads and
                         status responses (e.g. ``gmail_read``).
      label:             User-facing label rendered in the wizard
                         checkbox row.
      group:             Coarse grouping (Gmail / Calendar / Drive / etc.)
                         — drives the wizard's two-column layout.
      services:          OAuth service ids from server.py's
                         ``_GOOGLE_SCOPE_REGISTRY``. The wizard's
                         ``/begin`` request takes the union across
                         selected capabilities.
      scopes:            Full OAuth scope URLs this capability requires.
                         A capability is "granted" iff every scope here
                         appears in ``profile.scopes``.
      mcp_permissions:   ``--permissions`` arguments for workspace-mcp.
                         Multiple capabilities can contribute (e.g.
                         calendar_read + calendar_write both pass
                         ``calendar:read``).
      default_on:        Whether this capability is pre-checked in the
                         wizard. Only gmail_read + calendar_read are
                         default_on (legacy gog parity).
      restricted:        Whether the scope hits Google's "restricted
                         scope" verification track on personal accounts.
                         Surfaced under the wizard's Advanced disclosure.
    """

    id: str
    label: str
    group: str
    services: tuple[str, ...]
    scopes: frozenset[str]
    mcp_permissions: tuple[str, ...]
    default_on: bool = False
    restricted: bool = False


def _scope(name: str) -> str:
    """Shorthand for the canonical Google scope URL prefix."""
    return f"https://www.googleapis.com/auth/{name}"


#: The capability catalog. Order is the wizard's render order.
#:
#: Grouping ("Gmail" / "Calendar" / "Drive" / "Sheets" / "Docs" / "Advanced")
#: drives the two-column layout in the picker. Within a group, read comes
#: before write so the Plex-test user reads top-down ("first read, then
#: send", "first read, then edit").
GOOGLE_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="gmail_read",
        label="Read Gmail",
        group="Gmail",
        services=("gmail_readonly",),
        scopes=frozenset({_scope("gmail.readonly")}),
        mcp_permissions=("gmail:readonly",),
        default_on=True,
    ),
    Capability(
        id="gmail_send",
        label="Send Gmail",
        group="Gmail",
        services=("gmail",),  # bundle: send + readonly
        scopes=frozenset({_scope("gmail.send")}),
        mcp_permissions=("gmail:send",),
    ),
    Capability(
        id="calendar_read",
        label="Read your calendar",
        group="Calendar",
        services=("calendar_readonly",),
        scopes=frozenset({_scope("calendar.readonly")}),
        mcp_permissions=("calendar:read",),
        default_on=True,
    ),
    Capability(
        id="calendar_write",
        label="Create and edit calendar events",
        group="Calendar",
        services=("calendar",),
        scopes=frozenset({_scope("calendar")}),
        mcp_permissions=("calendar:read", "calendar:write"),
    ),
    Capability(
        id="drive_read",
        label="Read your Google Drive files",
        group="Drive",
        services=("drive_readonly",),
        scopes=frozenset({_scope("drive.readonly")}),
        mcp_permissions=("drive:readonly",),
    ),
    Capability(
        id="drive_write",
        label="Upload to / edit Drive files",
        group="Drive",
        services=("drive",),  # drive.file — narrow, bot-created + shared only
        scopes=frozenset({_scope("drive.file")}),
        mcp_permissions=("drive:file",),
    ),
    Capability(
        id="sheets_read",
        label="Read Google Sheets",
        group="Sheets",
        services=("sheets_readonly",),
        scopes=frozenset({_scope("spreadsheets.readonly")}),
        mcp_permissions=("sheets:read",),
    ),
    Capability(
        id="sheets_write",
        label="Edit Google Sheets",
        group="Sheets",
        services=("sheets",),
        scopes=frozenset({_scope("spreadsheets")}),
        mcp_permissions=("sheets:read", "sheets:write"),
    ),
    Capability(
        id="docs_read",
        label="Read Google Docs",
        group="Docs",
        services=("docs_readonly",),
        scopes=frozenset({_scope("documents.readonly")}),
        mcp_permissions=("docs:read",),
    ),
    Capability(
        id="docs_write",
        label="Edit Google Docs",
        group="Docs",
        services=("docs",),
        scopes=frozenset({_scope("documents")}),
        mcp_permissions=("docs:read", "docs:write"),
    ),
    # ── Advanced disclosure (restricted scopes — verification track) ────
    Capability(
        id="gmail_modify",
        label="Manage Gmail labels and archive (full mailbox)",
        group="Advanced",
        services=("gmail_modify",),
        scopes=frozenset({_scope("gmail.modify")}),
        mcp_permissions=("gmail:modify",),
        restricted=True,
    ),
    Capability(
        id="drive_full",
        label="Read and edit ALL Drive files",
        group="Advanced",
        services=("drive_full",),
        scopes=frozenset({_scope("drive")}),
        mcp_permissions=("drive",),
        restricted=True,
    ),
)


#: Sensible default (matches legacy gog Plex-test path).
DEFAULT_CAPABILITY_IDS: tuple[str, ...] = ("gmail_read", "calendar_read")


# Lookup index — ``id -> Capability``.
_CAPABILITY_INDEX: dict[str, Capability] = {c.id: c for c in GOOGLE_CAPABILITIES}


def capability_by_id(cap_id: str) -> Capability | None:
    """Look up a capability by id; None if unknown."""
    return _CAPABILITY_INDEX.get(cap_id)


def capabilities_for_ids(cap_ids: list[str] | tuple[str, ...]) -> list[Capability]:
    """Resolve a list of capability ids to Capability objects. Unknown ids
    are silently dropped (the wizard validates against the catalog, so
    they shouldn't reach here)."""
    return [_CAPABILITY_INDEX[i] for i in cap_ids if i in _CAPABILITY_INDEX]


# ── Capability ↔ scope / service / MCP-arg transforms ───────────────────────


def services_for_capabilities(cap_ids: list[str] | tuple[str, ...]) -> list[str]:
    """Union of OAuth service ids across the selected capabilities.

    Output is the value passed to ``/api/admin/onboard/google/begin`` as
    ``services``. The wizard's ``_services_to_scopes`` resolver expands
    them to scope URLs."""
    out: list[str] = []
    seen: set[str] = set()
    for c in capabilities_for_ids(cap_ids):
        for svc in c.services:
            if svc not in seen:
                seen.add(svc)
                out.append(svc)
    return out


def scopes_for_capabilities(cap_ids: list[str] | tuple[str, ...]) -> set[str]:
    """Union of OAuth scope URLs across the selected capabilities."""
    out: set[str] = set()
    for c in capabilities_for_ids(cap_ids):
        out |= c.scopes
    return out


def mcp_extra_args_for_capabilities(cap_ids: list[str] | tuple[str, ...]) -> list[str]:
    """Build the workspace-mcp ``--permissions`` argument list from the
    selected capabilities.

    Special case: when ONLY readonly capabilities are selected, return
    ``["--read-only"]`` — the server's read-only flag is shorter and
    matches the previous READ skill's wire format.
    """
    capabilities = capabilities_for_ids(cap_ids)
    if not capabilities:
        return []

    # All-read shortcut (matches the hidden _read skill's prior shape).
    if all(not c.id.endswith(("_send", "_write", "_modify", "_full"))
           for c in capabilities):
        return ["--read-only"]

    perms: list[str] = []
    seen: set[str] = set()
    for c in capabilities:
        for p in c.mcp_permissions:
            if p not in seen:
                seen.add(p)
                perms.append(p)
    return ["--permissions", *perms]


def derive_granted_capabilities(scopes: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    """Given the bot's currently granted OAuth scopes, return the list of
    capability ids that are fully covered.

    A capability is "granted" iff every scope in ``capability.scopes`` is
    present in the input set. Order matches GOOGLE_CAPABILITIES.

    Note: ``gmail.modify`` does NOT imply ``gmail.send`` etc. in Google's
    model — they're independent scope grants. Each capability's coverage
    is checked literally.
    """
    granted_scopes = set(scopes)
    return [c.id for c in GOOGLE_CAPABILITIES if c.scopes.issubset(granted_scopes)]


def summarize_capabilities(cap_ids: list[str] | tuple[str, ...]) -> str:
    """One-line summary for the per-bot status chip.

    Examples:
      []                              → "not connected"
      ["gmail_read"]                  → "read Gmail"
      ["gmail_read", "calendar_read"] → "read"
      ["gmail_read", "calendar_read", "gmail_send"] → "read + send Gmail"
      [all read + all write]          → "read + write"
      everything else                 → coarse category like "read + write"
                                        or "custom"
    """
    if not cap_ids:
        return "not connected"

    ids = set(cap_ids)
    all_reads = {c.id for c in GOOGLE_CAPABILITIES
                 if c.id.endswith("_read") and not c.restricted}
    all_writes = {c.id for c in GOOGLE_CAPABILITIES
                  if c.id.endswith(("_send", "_write")) and not c.restricted}

    # Pure read of the default pair (gmail_read + calendar_read) —
    # legacy gog shape.
    if ids == {"gmail_read", "calendar_read"}:
        return "read"

    # All reads, no writes.
    if ids.issubset(all_reads) and ids:
        if ids == all_reads:
            return "read"
        # Subset of reads — surface which apps.
        groups = sorted({_CAPABILITY_INDEX[i].group.lower() for i in ids})
        return "read " + " + ".join(groups)

    # All reads + all writes (the prior "Write" skill's full set).
    if all_reads.issubset(ids) and all_writes.issubset(ids):
        return "read + write"

    # Mixed.
    return "custom"


def capability_summary_entry(
    scopes: set[str] | list[str] | tuple[str, ...],
) -> dict[str, Any] | None:
    """Build the Skills-matrix capability-summary entry for a scope set.

    Returns ``{"summary": str, "labels": [str, ...]}`` — the shape the
    unified-Google catalog chip consumes via /api/skills/pod's
    ``capability_summaries`` — or ``None`` when the scopes map to no
    recognised capability (Workspace effectively not configured).

    This is the Path-C (``service_account_dwd``) resolver: those bots
    configure Workspace via ``network.json::google_integration.scopes``,
    NOT an OAuth profile, so the profile-based status resolver reports
    them as ``not_installed`` even though they are fully configured.
    Deriving the summary from the configured scope set lets the catalog
    row render the installed chip for them too. (Path A — OAuth-profile
    bots — produces the equivalent ``{summary, labels}`` shape directly
    from its :class:`InstallStatus` in the /api/skills/pod enrichment loop.)

    ``summary`` and ``labels`` are derived from the SAME scope→capability
    mapping the wizard and per-bot status chip use (:func:`derive_granted_
    capabilities` → :func:`summarize_capabilities`), so a Path-C bot's
    chip reads identically to a Path-A bot with the same grants.
    """
    caps = derive_granted_capabilities(scopes)
    if not caps:
        return None
    summary = summarize_capabilities(caps)
    if not summary or summary == "not connected":
        return None
    labels = [c.label for c in capabilities_for_ids(caps)]
    return {"summary": summary, "labels": labels}


def resolve_capability_summary_entry(
    bot_id: str, *, bot_cfg: "dict[str, Any] | None" = None,
) -> dict[str, Any] | None:
    """Per-bot ``{summary, labels}`` entry for /api/skills/pod's
    ``capability_summaries["google"]`` — or ``None`` when the bot has no
    Workspace config (the catalog row then renders "+ Add").

    Resolves BOTH config paths, in order:

    * **Path A** — OAuth profile (``free_gmail_oauth`` / legacy gog).
      The profile's consented scopes drive an :class:`InstallStatus`; we
      surface its ``capability_summary`` + per-capability labels (the
      labels are load-bearing when the summary collapses to the opaque
      ``"custom"`` — the tooltip enumerates them).
    * **Path C** — ``service_account_dwd``. These bots configure Workspace
      via ``network.json::bots.<id>.google_integration`` (subject +
      domain-wide delegation), NOT an OAuth profile, so Path A reports
      them ``not_installed``. We derive the summary from their configured
      ``google_integration.scopes`` instead, gating on the authoritative
      ``mode`` field (NOT the raw inventory matrix, whose ``google`` entry
      is OC's bundled Gemini LLM plugin) so Gemini-only bots don't
      false-positive.

    ``bot_cfg`` is the bot's ``network.json`` entry (carries
    ``google_integration``). The whole resolution is best-effort: any
    error yields ``None`` so one bad bot never blanks the catalog.
    """
    try:
        # Path A — OAuth profile. ``resolve_status_with_default_readers``
        # is scope-agnostic (no closure capture); see its docstring for
        # why /api/skills/pod can't reach create_app's reader closures.
        status = resolve_status_with_default_readers(bot_id)
        summary = (status.capability_summary or "").strip()
        if summary and summary != "not connected":
            labels = [c.label for c in capabilities_for_ids(
                status.granted_capabilities or [])]
            return {"summary": summary, "labels": labels}

        # Path C — service_account_dwd configured scopes.
        gi = (bot_cfg or {}).get("google_integration") or {}
        if gi.get("mode") == "service_account_dwd":
            return capability_summary_entry(gi.get("scopes") or [])
        return None
    except Exception:
        return None


# ── Install status ──────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of the bot's current install state.

    State machine:
      * ``oauth_client_missing`` — pod-level GCP client not configured.
      * ``not_installed``        — no OAuth profile + no MCP entry.
      * ``oauth_pending``        — wizard partially run, e.g. user did
        OAuth but ``/complete`` didn't finish, or profile is in
        ``reauth_required`` state.
      * ``needs_provision``      — OAuth profile good but
        ``mcp.servers.google_workspace`` is absent (bot booted on pre-
        skill build, or revoked mid-life). Re-run ``/complete``.
      * ``consumer_unreachable`` — MCP entry present but keystore slots
        empty.
      * ``active``               — all four stages green.
      * ``unknown``              — pre-flight read failed.

    The ``granted_capabilities`` field reflects what the bot CAN do
    today; the catalog row renders the per-bot chip's label from
    :func:`summarize_capabilities` over this list.
    """

    bot_id: str
    status: str
    google_account: str | None = None
    granted_capabilities: list[str] = field(default_factory=list)
    capability_summary: str = ""
    granted_scopes: list[str] = field(default_factory=list)
    has_oauth_profile: bool = False
    mcp_server_present: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": GOOGLE_SKILL_ID,
            "status": self.status,
            "google_account": self.google_account,
            "granted_capabilities": list(self.granted_capabilities),
            "capability_summary": self.capability_summary,
            "granted_scopes": list(self.granted_scopes),
            "has_oauth_profile": self.has_oauth_profile,
            "mcp_server_present": self.mcp_server_present,
            "error": self.error,
        }


# ── Install plan ────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    id: str
    label: str
    endpoint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    fields: list[dict[str, Any]] = field(default_factory=list)
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    access_panel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "fields": list(self.fields),
            "capabilities": list(self.capabilities),
            "access_panel": self.access_panel,
        }


def _capabilities_payload_for_picker(
    granted_capabilities: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Serialise GOOGLE_CAPABILITIES for the wizard's checkbox renderer.

    ``checked`` reflects:
      * Already-granted capabilities (modify-an-existing-install case), or
      * Sensible defaults when nothing is granted yet.
    """
    if granted_capabilities is None:
        checked = set(DEFAULT_CAPABILITY_IDS)
    else:
        # If the bot already has capabilities, pre-check them so the
        # picker shows current state and the operator can add/remove.
        checked = set(granted_capabilities) or set(DEFAULT_CAPABILITY_IDS)

    return [
        {
            "id": c.id,
            "label": c.label,
            "group": c.group,
            "checked": c.id in checked,
            "default_on": c.default_on,
            "restricted": c.restricted,
        }
        for c in GOOGLE_CAPABILITIES
    ]


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Build the ordered install plan for ``status``.

    Routing:
      * ``oauth_client_missing`` → one step: configure pod GCP client.
      * ``not_installed`` / ``oauth_pending`` → pick_capabilities → oauth → complete.
      * ``needs_provision`` / ``consumer_unreachable`` → complete only
        (capabilities derived from existing profile).
      * ``active`` / ``unknown`` → empty plan.

    When the bot is already installed, ``pick_capabilities`` pre-checks
    the current granted capabilities so the operator can adjust the set
    rather than starting from zero.
    """
    if status.status == "oauth_client_missing":
        return [
            InstallStep(
                id="configure_oauth_client",
                label="Set up Google for this pod",
                endpoint="/api/admin/onboard/google/configure",
                payload={"hint": "one-time pod-wide GCP client setup"},
            )
        ]

    if status.status == "unknown":
        return []

    if status.status == "active":
        # Re-running the wizard on an already-installed bot lets the
        # operator MODIFY capabilities (or just re-OAuth). The picker
        # comes pre-checked with the current grant set. The picker's
        # submit handler reads ``currently_granted`` from the step's
        # payload and SKIPS the oauth step when the picked set is a
        # subset of the currently-granted set — no need to bounce the
        # operator through Google's popup just to re-confirm scopes
        # they already have. (Google's consent screen would skip
        # silently anyway, but the popup-then-close is disorienting;
        # spec §13.1 Q3.)
        return [
            InstallStep(
                id="pick_capabilities",
                label="Adjust what the bot can do",
                endpoint=f"/api/skills/install/{GOOGLE_SKILL_ID}/pick-capabilities",
                payload={
                    "bot_id": status.bot_id,
                    "currently_granted": list(status.granted_capabilities or []),
                },
                capabilities=_capabilities_payload_for_picker(
                    status.granted_capabilities,
                ),
            ),
            InstallStep(
                id="oauth",
                label="Sign in with Google (only if new permissions added)",
                endpoint="/api/admin/onboard/google/begin",
                payload={
                    "bot_id": status.bot_id,
                    "services": list(services_for_capabilities(
                        status.granted_capabilities or DEFAULT_CAPABILITY_IDS,
                    )),
                },
            ),
            InstallStep(
                id="complete",
                label="Apply the changes",
                endpoint=f"/api/skills/install/{GOOGLE_SKILL_ID}/complete",
                payload={
                    "bot_id": status.bot_id,
                    "capabilities": list(
                        status.granted_capabilities or DEFAULT_CAPABILITY_IDS,
                    ),
                },
            ),
        ]

    if status.status in ("needs_provision", "consumer_unreachable"):
        return [
            InstallStep(
                id="complete",
                label="Connect the bot to Google",
                endpoint=f"/api/skills/install/{GOOGLE_SKILL_ID}/complete",
                payload={
                    "bot_id": status.bot_id,
                    # Reuse existing capabilities — no new OAuth needed.
                    "capabilities": list(status.granted_capabilities),
                },
            )
        ]

    # not_installed / oauth_pending — full wizard.
    plan: list[InstallStep] = []
    plan.append(
        InstallStep(
            id="pick_capabilities",
            label="Pick what the bot should be able to do",
            endpoint=f"/api/skills/install/{GOOGLE_SKILL_ID}/pick-capabilities",
            payload={"bot_id": status.bot_id},
            capabilities=_capabilities_payload_for_picker(
                status.granted_capabilities,
            ),
        )
    )
    plan.append(
        InstallStep(
            id="oauth",
            label="Sign in with Google",
            endpoint="/api/admin/onboard/google/begin",
            payload={
                "bot_id": status.bot_id,
                # The frontend overwrites this with the picker's actual
                # output before POSTing. Default keeps the legacy
                # shape if a caller never visited pick_capabilities.
                "services": list(services_for_capabilities(DEFAULT_CAPABILITY_IDS)),
            },
        )
    )
    plan.append(
        InstallStep(
            id="complete",
            label="Connect the bot to Google",
            endpoint=f"/api/skills/install/{GOOGLE_SKILL_ID}/complete",
            payload={
                "bot_id": status.bot_id,
                # Same — overwritten by the wizard with the picker's
                # final capability set.
                "capabilities": list(DEFAULT_CAPABILITY_IDS),
            },
        )
    )
    return plan


# ── Reader contracts (re-exported from shared module) ───────────────────────


ProfileReader = _shared.ProfileReader
ClientReader = _shared.ClientReader
OcConfigReader = _shared.OcConfigReader
KeystoreSlotReader = _shared.KeystoreSlotReader


# ── Status resolver ─────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_oauth_profile: ProfileReader,
    read_oauth_client: ClientReader,
    read_oc_config: OcConfigReader,
    read_keystore_slot: KeystoreSlotReader | None = None,
) -> InstallStatus:
    """Four-stage status resolver.

    Distinct from the hidden Read/Write resolvers: this one doesn't
    check against a fixed REQUIRED_SCOPES set. A bot is "active" if it
    has ANY recognised capability granted + an MCP server entry +
    healthy keystore slots. The set of granted capabilities (which the
    catalog chip renders) is derived from ``profile.scopes``.

    Legacy ``gog``-installed bots are picked up automatically: their
    profile has ``gmail.readonly`` + ``calendar.readonly`` scopes; the
    resolver maps that to ``[gmail_read, calendar_read]`` and reports
    active (assuming stage 3+4 also pass).
    """
    # Stage 1: OAuth client.
    try:
        client = read_oauth_client(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"client_read_failed: {exc.__class__.__name__}: {exc}",
        )
    if not client or not client.get("client_id"):
        return InstallStatus(bot_id=bot_id, status="oauth_client_missing")

    # Stage 2: OAuth profile.
    try:
        profile = read_oauth_profile(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"profile_read_failed: {exc.__class__.__name__}: {exc}",
        )
    if not profile:
        return InstallStatus(bot_id=bot_id, status="not_installed")

    if profile.get("status") == "reauth_required":
        granted_scopes = list(profile.get("scopes") or [])
        granted_caps = derive_granted_capabilities(granted_scopes)
        return InstallStatus(
            bot_id=bot_id, status="oauth_pending",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=granted_scopes,
            granted_capabilities=granted_caps,
            capability_summary=summarize_capabilities(granted_caps),
            error="reauth_required",
        )

    granted_scopes = list(profile.get("scopes") or [])
    granted_caps = derive_granted_capabilities(granted_scopes)

    # A profile with no recognised capabilities is broken — treat as
    # not_installed so the wizard re-runs (rather than reporting active
    # with an empty chip).
    if not granted_caps:
        return InstallStatus(
            bot_id=bot_id, status="not_installed",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=granted_scopes,
            granted_capabilities=[],
            capability_summary=summarize_capabilities([]),
        )

    # Stage 3: MCP server entry.
    try:
        oc, err = read_oc_config(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"oc_read_failed: {exc.__class__.__name__}: {exc}",
        )
    if oc is None:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=err or "oc_read_failed",
        )
    servers = ((oc.get("mcp") or {}).get("servers") or {})
    if not isinstance(servers.get(GOOGLE_MCP_SERVER_ID), dict):
        return InstallStatus(
            bot_id=bot_id, status="needs_provision",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=granted_scopes,
            granted_capabilities=granted_caps,
            capability_summary=summarize_capabilities(granted_caps),
        )

    # Stage 4: keystore slots.
    if read_keystore_slot is not None:
        for slot in (
            keystore_slot_client_id_for(bot_id),
            keystore_slot_client_secret_for(bot_id),
            keystore_slot_credentials_dir_for(bot_id),
        ):
            try:
                value = read_keystore_slot(slot)
            except Exception:
                value = None
            if not value:
                return InstallStatus(
                    bot_id=bot_id, status="consumer_unreachable",
                    has_oauth_profile=True,
                    mcp_server_present=True,
                    google_account=profile.get("google_account"),
                    granted_scopes=granted_scopes,
                    granted_capabilities=granted_caps,
                    capability_summary=summarize_capabilities(granted_caps),
                    error=f"keystore_slot_empty:{slot}",
                )

    return InstallStatus(
        bot_id=bot_id, status="active",
        has_oauth_profile=True,
        mcp_server_present=True,
        google_account=profile.get("google_account"),
        granted_scopes=granted_scopes,
        granted_capabilities=granted_caps,
        capability_summary=summarize_capabilities(granted_caps),
    )


def resolve_status_with_default_readers(bot_id: str) -> InstallStatus:
    """Resolve status using default disk-backed readers — no injection needed.

    Convenience entry point for callers that don't have access to the
    ``create_app`` scope's closure-based readers. The ``/api/skills/pod``
    enrichment loop in ``server.py`` lives in ``_register_skills_routes``,
    a sibling top-level function that can't see ``_read_google_oauth_profile``
    etc. — it calls this helper instead.

    Reuses default readers from ``google_workspace_token_shim`` and
    ``_oc_install_common.read_oc_config``. Keystore lookup is best-effort:
    if the keystore module isn't reachable, the resolver skips stage 4
    (matches the route-layer reader's ``<unreachable_assume_present>``
    sentinel).
    """
    from . import google_workspace_token_shim as _shim
    from . import _oc_install_common as _oc_common

    def _read_keystore_slot(slot: str) -> "str | None":
        try:
            from evolve_admin.keystore import KeystoreManager
            from ..config import load_network, DEFAULT_SHARED_DIR
            from pathlib import Path
            net = load_network()
            shared_dir = Path(net.get("sharedDir") or DEFAULT_SHARED_DIR)
            return KeystoreManager(shared_dir).get_value(slot)
        except Exception:
            return "<keystore_unreachable_assume_present>"

    return resolve_status(
        bot_id,
        read_oauth_profile=_shim._default_profile_reader,
        read_oauth_client=_shim._default_client_reader,
        read_oc_config=_oc_common.read_oc_config,
        read_keystore_slot=_read_keystore_slot,
    )


# ── InstallMcpServer / RemoveMcpServer payloads ─────────────────────────────


def build_install_mcp_action_payload(
    bot_id: str, capabilities: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Build the InstallMcpServer payload with dynamic extra_args.

    The ``capabilities`` parameter is the picker's output — the union of
    selected capability ids. The extra_args list is derived via
    :func:`mcp_extra_args_for_capabilities`.
    """
    return {
        "bot_id": bot_id,
        "server_id": GOOGLE_MCP_SERVER_ID,
        "catalog_id": GOOGLE_CATALOG_ID,
        "env_bindings": {
            "GOOGLE_OAUTH_CLIENT_ID": f"keystore:{keystore_slot_client_id_for(bot_id)}",
            "GOOGLE_OAUTH_CLIENT_SECRET": f"keystore:{keystore_slot_client_secret_for(bot_id)}",
            "WORKSPACE_MCP_CREDENTIALS_DIR": f"keystore:{keystore_slot_credentials_dir_for(bot_id)}",
        },
        "extra_args": mcp_extra_args_for_capabilities(capabilities),
    }


def build_remove_mcp_action_payload(bot_id: str) -> dict[str, Any]:
    """Same as the shared module — Read/Write/unified all share the entry."""
    return _shared.build_remove_mcp_action_payload(bot_id)


# ── Access panel (template — wizard renders per-capability detail) ──────────


GOOGLE_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GOOGLE_SKILL_ID,
    "skill_display_name": "Google",
    "summary": (
        "Connect this bot to your Google account so it can read or work "
        "with your Gmail, Calendar, Drive files, Sheets, and Docs. The "
        "next step lets you pick exactly what the bot is allowed to do."
    ),
    # The will/wont lists are CAPABILITY-DEPENDENT for the unified
    # skill. The wizard renders per-selected-capability lines based on
    # the picker's output. These template values are shown only on the
    # catalog detail page (before the picker runs).
    "will": [
        "Read your Gmail (and Sheets, Docs, Drive, Calendar) — when you "
        "grant those capabilities",
        "Send Gmail or edit Calendar events — only when you grant the "
        "matching write capability",
        "Show as a connected Google app under "
        "https://myaccount.google.com/permissions",
    ],
    "wont": [
        "Do anything you haven't granted on the next screen",
        "Access your Google Photos, Contacts, or other Google products",
        "Send Gmail from any address other than the one you signed in with",
        "Share your access with anyone outside this bot",
        "Change your Google account password, recovery options, or 2FA",
    ],
    "where_credentials_live": (
        "Your sign-in is stored only on this bot's user account on your "
        "machine — never centralised, never sent off-pod. You can revoke "
        "access at any time from this page, or at "
        "https://myaccount.google.com/permissions."
    ),
}


# ── Skill registry entry ────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": GOOGLE_SKILL_ID,
    "display_name": GOOGLE_ACCESS_PANEL["skill_display_name"],
    "summary": GOOGLE_ACCESS_PANEL["summary"],
    "access_panel": dict(GOOGLE_ACCESS_PANEL),
    "category": "productivity",
    "catalog_id": GOOGLE_CATALOG_ID,
    "mcp_server_id": GOOGLE_MCP_SERVER_ID,
    "default_capabilities": list(DEFAULT_CAPABILITY_IDS),
    "capabilities_catalog": [
        {
            "id": c.id,
            "label": c.label,
            "group": c.group,
            "default_on": c.default_on,
            "restricted": c.restricted,
        }
        for c in GOOGLE_CAPABILITIES
    ],
    # Combined rate limits — the per-bot soft caps the existing rate
    # limiter applies regardless of which capabilities are active.
    "rate_limits": {
        "gmail_reads_per_minute": 120,
        "gmail_send_per_minute": 60,
        "calendar_reads_per_minute": 200,
        "calendar_writes_per_minute": 100,
        "drive_reads_per_minute": 100,
        "drive_uploads_per_minute": 30,
        "sheets_reads_per_minute": 60,
        "sheets_appends_per_minute": 30,
        "docs_reads_per_minute": 60,
        "docs_edits_per_minute": 30,
    },
}
