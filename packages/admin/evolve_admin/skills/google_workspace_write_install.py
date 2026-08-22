"""evolve_admin.skills.google_workspace_write_install — Workspace (Write) install.

Spec: ``docs/spec-google-workspace-suite-2026-06-04.md``.
Vetting: ``docs/vetting-workspace-mcp-2026-06-04.md`` (chose
``taylorwilsdon/google_workspace_mcp`` aka PyPI ``workspace-mcp``).

This is the **first** install module that closes the runtime-consumer gap
the 2026-05-30 deep audit identified for the ``gog`` skill family. The
previous shape wrote a real OAuth token to ``auth-profiles.json`` and
declared success — but no consumer in OC's bundle reads that file for
Gmail/Calendar/Drive/Sheets/Docs APIs. This module bridges to a vetted
external MCP server (Phase 1 of the spec).

**Status: internal-only shared infrastructure.** The catalog-listed
Google skill is now the unified ``google_install`` module (PR #2231),
which collapses the split-skill design into one row with an in-wizard
capability picker. This module remains as the source of:

  * ``CompletionResult`` dataclass (per-stage diagnostic for /complete)
  * ``preflight_check`` (Gmail + Calendar + Drive 3-call validator)
  * Keystore-slot helpers (``keystore_slot_client_id_for`` etc.)
  * ``build_remove_mcp_action_payload`` (RemoveMcpServer payload)
  * Reader-type aliases (ProfileReader, ClientReader, OcConfigReader,
    KeystoreSlotReader)

The pre-pivot ``[--permissions ...]`` extra_args constants stay so the
legacy ``google_workspace_write`` route still resolves with the original
write-everything semantics (kept for backward-compat with deep links;
the catalog list no longer surfaces it). The Read sibling was deleted
as vestigial.

Installing Write on a bot that has Read already-installed is a clean
replace: the InstallMcpServer applier overwrites ``mcp.servers.google_workspace``
with the broader scope set.

Architecture
------------

::

   ┌──────────────────────────────────────────────────────────────────┐
   │ Wizard flow (the user-facing 3-step modal)                       │
   │                                                                  │
   │   Step 1 — pick account type                                     │
   │   Step 2 — see capability review (access panel)                  │
   │   Step 3 — OAuth popup; wizard polls /poll for state             │
   │                                                                  │
   │           ↓ after callback succeeds                              │
   │                                                                  │
   │   POST /api/skills/install/google_workspace_write/complete       │
   │           ↓                                                      │
   │   complete_install(bot_id) does:                                 │
   │     1. Pre-flight: Gmail.getProfile + Calendar.calendarList.list │
   │        + Drive.about.get — all three must pass                   │
   │     2. write keystore slots:                                     │
   │           gws-client-id-<bot>     → client_id                    │
   │           gws-client-secret-<bot> → client_secret                │
   │           gws-creds-dir-<bot>     → absolute credentials dir     │
   │     3. token_shim.write_credentials_for_bot(bot_id)              │
   │     4. InstallMcpServer proposal (catalog_id="google_workspace", │
   │        server_id="google_workspace", env_bindings → keystore     │
   │        refs, extra_args=WRITE_FLAGS)                             │
   │     5. kickstart gateway                                         │
   │                                                                  │
   │   Failure at any step rolls back the previous steps so the bot   │
   │   never lands in a partial install.                              │
   └──────────────────────────────────────────────────────────────────┘

Status machine
--------------

The four-stage resolver (per spec §9.6) returns one of:

  * ``oauth_client_missing`` — pod-level GCP client not configured;
    operator must run the one-time setup at ``/api/admin/onboard/google/configure``.
  * ``oauth_pending``        — client configured but no profile, or
    profile present without the write scope set we need.
  * ``mcp_not_installed``    — profile is present + sufficient scope, but
    ``mcp.servers.google_workspace`` is absent (e.g., bot booted on a
    pre-skill build then upgraded).
  * ``consumer_unreachable`` — mcp.servers entry present but the
    keystore slots referenced are missing values (someone wiped the
    keystore).
  * ``active``               — all four stages green.
  * ``unknown``              — pre-flight read failed.

Pre-flight call (status==active) is deferred to the install path; the
resolver runs cheap on every status poll.

Trust-chain decisions
---------------------

1. **The skill owns scope validation, not OAuth itself.** The wizard at
   ``/api/admin/onboard/google/begin`` does the OAuth dance. We tell it
   which scopes to request via the ``services`` payload. The callback
   writes the profile. **After** that, ``complete_install`` validates the
   ``profile.scopes`` covers our required set; if not, the status falls
   to ``oauth_pending`` with ``error="scope_short"`` (the wizard re-prompts).

2. **Three keystore slots, all per-bot.** The credential is per-bot, but
   the OAuth client may be pod-wide-legacy or per-bot (resolved by the
   client reader). We store the resolved client_id + client_secret per-bot
   so the launcher's keystore lookup at exec time is unambiguous, even
   for bots on the legacy pod-wide client config.

3. **The credentials_dir path is also in the keystore.** The MCP server
   reads it from ``WORKSPACE_MCP_CREDENTIALS_DIR``. Putting the path in
   the keystore is semantically weird but uses the existing applier
   pipeline without a launcher change. The path is not secret; the
   keystore happens to be the only env-binding source the launcher
   supports today.

4. **Revoke is symmetric.** Per F2 in the deep audit, install + revoke
   must leave openclaw.json structurally identical to pre-install. This
   module's :func:`revoke_install` performs the inverse of
   :func:`complete_install` step-by-step.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Skill identifier ────────────────────────────────────────────────────────


#: Skill id surfaced in ``/api/skills/catalog`` and the Skills page.
GOOGLE_WORKSPACE_WRITE_SKILL_ID = "google_workspace_write"

#: Shared with the Read skill (same MCP server underneath).
GOOGLE_WORKSPACE_MCP_SERVER_ID = "google_workspace"

#: Catalog id for the InstallMcpServer applier. Added to
#: ``packages/analyzer/mcp_admin/catalog.py::default_entries()`` in a
#: sibling change.
GOOGLE_WORKSPACE_CATALOG_ID = "google_workspace"

#: HTTP timeout for a single Google API pre-flight call.
GOOGLE_HTTP_TIMEOUT_S = 10

#: Hard wall-clock budget for the WHOLE three-call pre-flight. Without it the
#: three sequential calls could sum to 3 × GOOGLE_HTTP_TIMEOUT_S; on a slow or
#: half-open network that, plus the access-token refresh in the /complete
#: handler, kept the single-threaded admin dev server blocked long enough for
#: the browser to give up — the server then crashed writing the response
#: (``OSError [Errno 57] Socket is not connected``) and the wizard painted an
#: all-✗ "couldn't check access" panel for an already-configured bot (live:
#: atlas, 2026-06-22). Mirrors the bounded-deadline shape of the github/status
#: preflight: a tight total deadline + an explicit timeout vs. connection-failure
#: distinction so the UI can say "try again" instead of "your setup is broken".
PREFLIGHT_DEADLINE_S = 12.0


# ── Scope contract ──────────────────────────────────────────────────────────


#: The services the wizard asks Google for when this skill installs. Maps
#: to service ids in ``server.py::_GOOGLE_SCOPE_REGISTRY``.
#:
#: ``gmail`` and ``calendar`` are the write-capable variants; ``drive``
#: is ``drive.file`` (per-file write — narrow). ``sheets`` and ``docs``
#: cover Google Sheets and Google Docs respectively.
WRITE_DEFAULT_SERVICES: tuple[str, ...] = (
    "gmail",
    "calendar",
    "drive",
    "sheets",
    "docs",
)


#: Full OAuth scope URLs the write skill MUST have to declare ``active``.
#: Checked at status-resolve time against ``profile.scopes``. If any is
#: missing the status drops to ``oauth_pending`` with ``error="scope_short"``.
WRITE_REQUIRED_SCOPES: frozenset[str] = frozenset({
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
})


#: The ``--permissions`` flags passed to the MCP server's CLI. Maps the
#: scope set above to the workspace-mcp permission-string language
#: (vetting doc §6). The launcher's wrapper script appends these as
#: ``extra_args`` to ``uvx workspace-mcp …``.
WRITE_MCP_EXTRA_ARGS: tuple[str, ...] = (
    "--permissions",
    "gmail:send",
    "gmail:readonly",
    "calendar:read",
    "calendar:write",
    "drive:file",
    "drive:readonly",
    "sheets:read",
    "sheets:write",
    "docs:read",
    "docs:write",
)


# ── Plain-language access panel (per spec §5.2) ─────────────────────────────


GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GOOGLE_WORKSPACE_WRITE_SKILL_ID,
    "skill_display_name": "Google Workspace — Read + Write",
    "summary": (
        "Lets this bot read AND write Gmail, calendar, Google Drive files, "
        "Sheets, and Docs. The bot can send email, create calendar events, "
        "upload files, and edit content — within the limits below."
    ),
    "will": [
        "Read your incoming Gmail so it can reply in context",
        "Send Gmail messages on your behalf — replies and new messages",
        "Read and write your Google Calendar — create, edit, and cancel events",
        "Upload new files to Google Drive and edit files it created or that "
        "you've shared with it",
        "Create new Google Sheets and Docs, and edit their contents",
        "See your Google account email so it knows which account to act as",
    ],
    "wont": [
        "Delete or permanently archive existing Drive files it did not create",
        "Read or edit Drive files that are not shared with this bot AND that "
        "the bot did not create itself",
        "Delete or modify Gmail messages other than the drafts it composes",
        "Forward your Gmail to anyone outside this bot",
        "Send email from any address other than the one you signed in with",
        "Access your Google Photos, Contacts, or other Google products",
        "Share your access with anyone outside this bot",
        "Change your Google account password, recovery options, or 2FA",
    ],
    "where_credentials_live": (
        "Your sign-in is stored only on this bot's user account on your "
        "machine — never centralised, never sent off-pod. You can revoke "
        "access at any time from this page, or at "
        "https://myaccount.google.com/permissions."
    ),
    "scopes_granted_user_facing": [
        "Send Gmail (and read it for context)",
        "Read and write your Google Calendar",
        "Upload to Google Drive and edit files it created or you've shared "
        "with it",
        "Create and edit Google Sheets and Docs",
    ],
    "default_services": list(WRITE_DEFAULT_SERVICES),
}


# ── Keystore slot naming ────────────────────────────────────────────────────


def keystore_slot_client_id_for(bot_id: str) -> str:
    """Per-bot keystore slot for the resolved OAuth client_id."""
    return f"gws-client-id-{bot_id}"


def keystore_slot_client_secret_for(bot_id: str) -> str:
    """Per-bot keystore slot for the resolved OAuth client_secret."""
    return f"gws-client-secret-{bot_id}"


def keystore_slot_credentials_dir_for(bot_id: str) -> str:
    """Per-bot keystore slot for the absolute credentials-dir path.

    Holds a path string, not a secret. The MCP launcher resolves it at
    exec time the same way it resolves secret env bindings; see vetting
    doc §6 and the module docstring for the design trade-off.
    """
    return f"gws-creds-dir-{bot_id}"


# ── Install status ──────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Workspace-Write install flow.

    State machine (see spec §9.6):

    * ``oauth_client_missing`` — no pod-level GCP client configured
      yet; the wizard's pod-setup step must run first.
    * ``oauth_pending``        — client configured but no profile (or
      profile lacks the required Write scopes — ``error="scope_short"``).
    * ``mcp_not_installed``    — profile good but ``mcp.servers.
      google_workspace`` is absent; ``complete_install`` will fix this.
    * ``consumer_unreachable`` — mcp.servers entry exists but one of
      the keystore slots is empty; install needs to re-run.
    * ``active``               — all four stages green.
    * ``unknown``              — pre-flight read failed.
    """

    bot_id: str
    status: str
    google_account: str | None = None
    granted_services: list[str] = field(default_factory=list)
    granted_scopes: list[str] = field(default_factory=list)
    mcp_server_present: bool = False
    has_oauth_profile: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": GOOGLE_WORKSPACE_WRITE_SKILL_ID,
            "status": self.status,
            "google_account": self.google_account,
            "granted_services": list(self.granted_services),
            "granted_scopes": list(self.granted_scopes),
            "mcp_server_present": self.mcp_server_present,
            "has_oauth_profile": self.has_oauth_profile,
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
    access_panel: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "payload": dict(self.payload),
            "fields": list(self.fields),
            "access_panel": self.access_panel,
        }


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Build the ordered list of install steps for ``status``.

    Routing:

    * ``oauth_client_missing`` → one step: configure pod-level GCP client.
      The wizard's pod-setup screen lives at
      ``/api/admin/onboard/google/configure``.
    * ``oauth_pending``        → three steps: account-type → capability
      review → OAuth, then complete.
    * ``mcp_not_installed``    → one step: complete (the OAuth profile
      is fine; just run the post-OAuth provisioning).
    * ``consumer_unreachable`` → same as ``mcp_not_installed`` (re-run
      complete; idempotent).
    * ``active`` / ``unknown`` → empty plan (UI shows success or error).
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

    if status.status in ("active", "unknown"):
        return []

    plan: list[InstallStep] = []

    if status.status in ("mcp_not_installed", "consumer_unreachable"):
        # OAuth is already done; we just need to provision the MCP.
        plan.append(
            InstallStep(
                id="complete",
                label="Connect the bot to Google Workspace",
                endpoint=f"/api/skills/install/{GOOGLE_WORKSPACE_WRITE_SKILL_ID}/complete",
                payload={"bot_id": status.bot_id},
            )
        )
        return plan

    # oauth_pending: full flow.
    plan.append(
        InstallStep(
            id="account_type",
            label="Pick your Google account type",
            endpoint=f"/api/skills/install/{GOOGLE_WORKSPACE_WRITE_SKILL_ID}/account-type",
            payload={"bot_id": status.bot_id},
            fields=[
                {
                    "name": "account_type",
                    "label": "Account type",
                    "type": "radio",
                    "options": [
                        {
                            "value": "free_gmail",
                            "label": "A free Gmail account (gmail.com)",
                        },
                        {
                            "value": "workspace",
                            "label": "A Google Workspace account "
                                     "(custom domain like example.com)",
                        },
                    ],
                    "help": (
                        "Not sure? If you sign in to Gmail with "
                        "someone@gmail.com, pick Free. If you sign in with "
                        "you@yourcompany.com — even if your company is just "
                        "you — pick Workspace."
                    ),
                },
            ],
        )
    )
    plan.append(
        InstallStep(
            id="capability_review",
            label="Review what the bot will be able to do",
            endpoint=f"/api/skills/install/{GOOGLE_WORKSPACE_WRITE_SKILL_ID}/capability-review",
            payload={"bot_id": status.bot_id},
            access_panel=dict(GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL),
        )
    )
    plan.append(
        InstallStep(
            id="oauth",
            label="Sign in with Google",
            endpoint="/api/admin/onboard/google/begin",
            payload={
                "bot_id": status.bot_id,
                "services": list(WRITE_DEFAULT_SERVICES),
            },
            access_panel=dict(GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL),
        )
    )
    plan.append(
        InstallStep(
            id="complete",
            label="Connect the bot to Google Workspace",
            endpoint=f"/api/skills/install/{GOOGLE_WORKSPACE_WRITE_SKILL_ID}/complete",
            payload={"bot_id": status.bot_id},
        )
    )
    return plan


# ── Reader contracts (injectable for testability) ───────────────────────────


#: Reader: ``(bot_id) -> dict | None`` — the OAuth profile from auth-profiles.
ProfileReader = Callable[[str], "dict[str, Any] | None"]

#: Reader: ``(bot_id) -> {client_id, client_secret, mode} | None`` —
#: resolved OAuth client.
ClientReader = Callable[[str], "dict[str, Any] | None"]

#: Reader: ``(bot_id) -> (dict | None, str | None)`` — bot's openclaw.json.
OcConfigReader = Callable[[str], "tuple[dict[str, Any] | None, str | None]"]

#: Reader: ``(slot) -> str | None`` — keystore value lookup.
KeystoreSlotReader = Callable[[str], "str | None"]


# ── Status resolver (per spec §9.6) ─────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_oauth_profile: ProfileReader,
    read_oauth_client: ClientReader,
    read_oc_config: OcConfigReader,
    read_keystore_slot: KeystoreSlotReader | None = None,
) -> InstallStatus:
    """Pure-Python status resolver.

    Stages 1-4 per spec §9.6. Reader callables are injectable so tests
    pass stubs; route layer wires real readers from ``server.py``.

    Never raises — captures reader exceptions as ``unknown``.
    """
    # Stage 1: pod-level OAuth client configured?
    try:
        client = read_oauth_client(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"client_read_failed: {exc.__class__.__name__}: {exc}",
        )
    if not client or not client.get("client_id"):
        return InstallStatus(bot_id=bot_id, status="oauth_client_missing")

    # Stage 2: OAuth profile present with sufficient scope?
    try:
        profile = read_oauth_profile(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"profile_read_failed: {exc.__class__.__name__}: {exc}",
        )
    if not profile:
        return InstallStatus(bot_id=bot_id, status="oauth_pending")
    if profile.get("status") == "reauth_required":
        return InstallStatus(
            bot_id=bot_id, status="oauth_pending",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=list(profile.get("scopes") or []),
            granted_services=list(profile.get("services") or []),
            error="reauth_required",
        )
    granted_scopes = set(profile.get("scopes") or [])
    missing_scopes = WRITE_REQUIRED_SCOPES - granted_scopes
    if missing_scopes:
        return InstallStatus(
            bot_id=bot_id, status="oauth_pending",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=sorted(granted_scopes),
            granted_services=list(profile.get("services") or []),
            error=f"scope_short:{','.join(sorted(missing_scopes))}",
        )

    # Stage 3: mcp.servers entry installed?
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
    mcp_entry = servers.get(GOOGLE_WORKSPACE_MCP_SERVER_ID)
    if not isinstance(mcp_entry, dict):
        return InstallStatus(
            bot_id=bot_id, status="mcp_not_installed",
            has_oauth_profile=True,
            google_account=profile.get("google_account"),
            granted_scopes=sorted(granted_scopes),
            granted_services=list(profile.get("services") or []),
        )

    # Stage 4: keystore slots healthy?
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
                    granted_scopes=sorted(granted_scopes),
                    granted_services=list(profile.get("services") or []),
                    error=f"keystore_slot_empty:{slot}",
                )

    return InstallStatus(
        bot_id=bot_id, status="active",
        has_oauth_profile=True,
        mcp_server_present=True,
        google_account=profile.get("google_account"),
        granted_scopes=sorted(granted_scopes),
        granted_services=list(profile.get("services") or []),
    )


# ── Pre-flight API calls ────────────────────────────────────────────────────


#: Error tokens that mean "the check couldn't run" (network/deadline) rather
#: than "the check ran and the bot's access is bad" (an HTTP status). The UI
#: keys off these to show a "try again" instead of a scary all-✗ panel.
PREFLIGHT_TRANSIENT_ERRORS = ("timeout", "connection_failed")


def is_transient_preflight_error(error: str | None) -> bool:
    """True iff ``error`` denotes a couldn't-run failure (network/deadline).

    Preflight errors are ``"<service>_preflight_<reason>"``; the reason is one
    of :data:`PREFLIGHT_TRANSIENT_ERRORS` for network/deadline failures or
    ``status_<code>`` when the call ran and Google rejected it.
    """
    if not error:
        return False
    return any(error.endswith(tok) for tok in PREFLIGHT_TRANSIENT_ERRORS)


def _google_get(
    url: str, access_token: str, *, timeout: float = GOOGLE_HTTP_TIMEOUT_S,
) -> tuple[int, dict | None, str | None]:
    """Authenticated GET against a Google API URL.

    Returns ``(http_status, json_body, error)``. On a network-level failure
    (status 0) ``error`` distinguishes ``"timeout"`` (deadline exceeded) from
    ``"connection_failed"`` — both are tooling/transient failures, NOT evidence
    the bot's Google access is broken. ``json_body`` may be None when the
    response isn't JSON.
    """
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}, None
            except Exception:
                return resp.status, None, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw else None, None
        except Exception:
            return e.code, None, None
    except urllib.error.URLError as e:
        # URLError wraps the underlying socket failure; a timeout reason is a
        # deadline, anything else is a connection failure. (HTTPError, caught
        # above, is a URLError subclass — order matters.)
        reason = getattr(e, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            log.debug("google_get: %s timed out after %.1fs", url, timeout)
            return 0, None, "timeout"
        log.debug("google_get: %s failed: %s", url, e)
        return 0, None, "connection_failed"
    except (TimeoutError, socket.timeout):
        log.debug("google_get: %s timed out after %.1fs", url, timeout)
        return 0, None, "timeout"
    except Exception as exc:
        log.debug("google_get: %s failed: %s", url, exc)
        return 0, None, "connection_failed"


def preflight_check(
    access_token: str, *, deadline_s: float = PREFLIGHT_DEADLINE_S,
) -> dict[str, Any]:
    """Run the three pre-flight calls Gmail + Calendar + Drive must pass.

    Per spec §4.2, all three must return 200 to declare success — the
    "credential lands somewhere" / "credential works" distinction the
    May incident drove home (F3 in the deep audit).

    The whole sequence is bounded by ``deadline_s`` (default
    :data:`PREFLIGHT_DEADLINE_S`): each call gets at most the remaining budget,
    and once the budget is gone we stop with a ``timeout`` error instead of
    issuing another blocking call. This keeps the single-threaded admin server
    from outliving the client (the ENOTCONN crash that drove this fix).

    Returns a dict with:
      * ``ok`` (bool) — True iff all three calls returned 200.
      * ``gmail_ok`` / ``calendar_ok`` / ``drive_ok`` (bool)
      * ``gmail_email`` (str | None) — from Gmail.users.getProfile
      * ``error`` (str | None) — short reason on failure; ends in one of
        :data:`PREFLIGHT_TRANSIENT_ERRORS` when the check couldn't run.
    """
    out: dict[str, Any] = {
        "ok": False,
        "gmail_ok": False,
        "calendar_ok": False,
        "drive_ok": False,
        "gmail_email": None,
        "error": None,
    }

    deadline = time.monotonic() + max(1.0, deadline_s)

    def _budget_get(url: str) -> tuple[int, dict | None, str | None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            return 0, None, "timeout"
        return _google_get(url, access_token, timeout=min(GOOGLE_HTTP_TIMEOUT_S, remaining))

    gmail_status, gmail_body, gmail_err = _budget_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
    )
    if gmail_err:
        out["error"] = f"gmail_preflight_{gmail_err}"
        return out
    if gmail_status == 200 and isinstance(gmail_body, dict):
        out["gmail_ok"] = True
        out["gmail_email"] = gmail_body.get("emailAddress")
    else:
        out["error"] = f"gmail_preflight_status_{gmail_status}"
        return out

    cal_status, _cal_body, cal_err = _budget_get(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1",
    )
    if cal_err:
        out["error"] = f"calendar_preflight_{cal_err}"
        return out
    if cal_status == 200:
        out["calendar_ok"] = True
    else:
        out["error"] = f"calendar_preflight_status_{cal_status}"
        return out

    drv_status, _drv_body, drv_err = _budget_get(
        "https://www.googleapis.com/drive/v3/about?fields=user",
    )
    if drv_err:
        out["error"] = f"drive_preflight_{drv_err}"
        return out
    if drv_status == 200:
        out["drive_ok"] = True
    else:
        out["error"] = f"drive_preflight_status_{drv_status}"
        return out

    out["ok"] = True
    return out


# ── Completion: provision MCP server after OAuth succeeds ───────────────────


@dataclass
class CompletionResult:
    """Outcome of :func:`complete_install`.

    * ``ok`` — True iff all five steps succeeded (preflight, keystore writes,
      token shim, InstallMcpServer proposal, gateway kickstart).
    * Each step's ``_done`` / ``_error`` field records its individual fate
      so the wizard's diagnostic panel can show which step failed.
    """

    bot_id: str
    ok: bool
    preflight_done: bool = False
    preflight_error: str | None = None
    keystore_done: bool = False
    keystore_error: str | None = None
    token_shim_done: bool = False
    token_shim_error: str | None = None
    mcp_install_done: bool = False
    mcp_install_error: str | None = None
    gateway_kickstart_done: bool = False
    gateway_kickstart_error: str | None = None
    google_account: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "ok": self.ok,
            "preflight": {
                "done": self.preflight_done,
                "error": self.preflight_error,
                # True when the preflight couldn't RUN (network/deadline) vs.
                # ran and found bad access — lets the wizard show "try again"
                # instead of a scary all-✗ on a configured bot.
                "transient": is_transient_preflight_error(self.preflight_error),
            },
            "keystore": {"done": self.keystore_done, "error": self.keystore_error},
            "token_shim": {"done": self.token_shim_done, "error": self.token_shim_error},
            "mcp_install": {"done": self.mcp_install_done, "error": self.mcp_install_error},
            "gateway_kickstart": {
                "done": self.gateway_kickstart_done,
                "error": self.gateway_kickstart_error,
            },
            "google_account": self.google_account,
        }


def build_install_mcp_action_payload(bot_id: str) -> dict[str, Any]:
    """Build the action payload for the InstallMcpServer proposal.

    Pure: no disk, no network — testable in isolation. The wizard route
    wraps this with proposal-creation machinery.

    Three env_bindings, all keystore refs (per the launcher's contract
    documented in ``mcp_admin/launcher.py:render_script``).
    """
    return {
        "bot_id": bot_id,
        "server_id": GOOGLE_WORKSPACE_MCP_SERVER_ID,
        "catalog_id": GOOGLE_WORKSPACE_CATALOG_ID,
        "env_bindings": {
            "GOOGLE_OAUTH_CLIENT_ID": f"keystore:{keystore_slot_client_id_for(bot_id)}",
            "GOOGLE_OAUTH_CLIENT_SECRET": f"keystore:{keystore_slot_client_secret_for(bot_id)}",
            "WORKSPACE_MCP_CREDENTIALS_DIR": f"keystore:{keystore_slot_credentials_dir_for(bot_id)}",
        },
        "extra_args": list(WRITE_MCP_EXTRA_ARGS),
    }


# ── Revoke ──────────────────────────────────────────────────────────────────


def build_remove_mcp_action_payload(bot_id: str) -> dict[str, Any]:
    """Build the action payload for the RemoveMcpServer proposal.

    Mirror of :func:`build_install_mcp_action_payload` for the revoke path.
    """
    return {
        "bot_id": bot_id,
        "server_id": GOOGLE_WORKSPACE_MCP_SERVER_ID,
    }


# ── Skill registry entry ────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": GOOGLE_WORKSPACE_WRITE_SKILL_ID,
    "display_name": GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["skill_display_name"],
    "summary": GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL["summary"],
    "access_panel": dict(GOOGLE_WORKSPACE_WRITE_ACCESS_PANEL),
    "default_services": list(WRITE_DEFAULT_SERVICES),
    "required_scopes": sorted(WRITE_REQUIRED_SCOPES),
    "catalog_id": GOOGLE_WORKSPACE_CATALOG_ID,
    "mcp_server_id": GOOGLE_WORKSPACE_MCP_SERVER_ID,
    # Rate-limit advertising per spec §7.2 — soft caps well below Google's
    # actual quotas, surfaced on the Skills page when the chip is yellow.
    "rate_limits": {
        "gmail_send_per_minute": 60,
        "calendar_writes_per_minute": 100,
        "drive_uploads_per_minute": 30,
        "sheets_appends_per_minute": 30,
        "docs_edits_per_minute": 30,
    },
}
