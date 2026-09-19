"""evolve_admin.skills.zoom_install — Zoom skill install module.

Spec: ``internal/spec-zoom-skill-2026-06-06.md``.
Shim package: ``packages/evolve-zoom-mcp/`` (Phase 2 PR #2285).

Architecture
------------

The Zoom skill is backed by an Evolve-owned stdio MCP shim
(``evolve-zoom-mcp``) that holds two confidential OAuth apps:

* **General App** (user-OAuth) — for the read-side tools proxied from
  Zoom's hosted MCP at ``mcp.zoom.us/mcp/zoom/streamable``.
* **Server-to-Server OAuth App** (optional) — for the write-side meeting
  tools (``create_meeting`` / ``update_meeting`` / ``delete_meeting``).

This module wires the install + status + revoke flows for the single
``mcp.servers.zoom`` entry. The actual OAuth dance + token refresh lives
inside the shim package; this module shells out to the shim's CLI
(``evolve-zoom-mcp login --code <code>``) to complete the dance.

Install flow:

  1. Operator creates the General App in Zoom Marketplace and supplies
     ``client_id`` / ``client_secret`` / ``redirect_url`` to the wizard
     (``POST /api/skills/install/zoom/set-oauth-client``).
  2. Wizard returns the authorize URL constructed from those values.
  3. Operator opens it, authorizes as the bot's intended Zoom user.
  4. Zoom redirects with ``?code=…`` — operator pastes the code into the
     wizard (``POST /api/skills/install/zoom/complete-oauth``).
  5. Wizard invokes ``evolve-zoom-mcp login --code <code>`` as the bot
     user. The shim exchanges the code for tokens, writes a refresh
     token to ``<bot_home>/.openclaw/zoom/credentials.json``.
  6. Wizard writes ``mcp.servers.zoom`` to the bot's openclaw.json and
     kickstarts the gateway.

Optionally, the operator submits S2S credentials in a second pass:

  * ``POST /api/skills/install/zoom/set-s2s-credentials`` validates by
    minting a test S2S access token, persists to keystore, layers the
    write-side env_bindings into ``mcp.servers.zoom``, kickstarts.

Status states:

* ``not_configured``       — no General-App keystore slots yet.
* ``oauth_pending``        — keystore slots present but credentials.json
                              missing (operator stopped after step 1 or 2).
* ``oauth_expired``        — credentials.json present but refresh token
                              rejected by Zoom (revoked, rotated by user).
* ``mcp_not_installed``    — OAuth complete but ``mcp.servers.zoom``
                              absent in openclaw.json (e.g. fresh deploy
                              hasn't run kickstart yet).
* ``read_only_configured`` — read side wired and live; S2S not set up.
                              Valid terminal state for operators who
                              don't need meeting creation.
* ``fully_configured``     — read + S2S both wired but tools/list probe
                              not yet attempted.
* ``active``               — read + S2S wired AND a tools/list probe
                              against the shim succeeded.
* ``mcp_unreachable``      — mcp.servers.zoom present but the shim
                              process can't be reached for a probe.
* ``s2s_invalid``          — S2S keystore slots present but creds were
                              rotated / revoked.
* ``unknown``              — read failed for an unclassified reason.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ── Identifiers ─────────────────────────────────────────────────────────────


ZOOM_SKILL_ID = "zoom"
ZOOM_SERVER_ID = "zoom"  # mcp.servers.zoom in the bot's openclaw.json
ZOOM_CATALOG_ID = "zoom"


# ── Scope contract ──────────────────────────────────────────────────────────


#: User-OAuth scopes the shim requests at authorize time. Pinned 2026-06-06
#: against Zoom's Marketplace scope picker. Drift CI script (spec §11.c)
#: will verify these against the live tool list.
ZOOM_USER_OAUTH_SCOPES: tuple[str, ...] = (
    "meeting:read:meeting",
    "meeting:read:list_meetings",
    "cloud_recording:read:list_user_recordings",
    "cloud_recording:read:list_recording_files",
    "cloud_recording:read:recording",
    "cloud_recording:read:meeting_transcript",
    "team_chat:read:list_user_messages",
    "user:read:user",
    "user:read:email",
)


# ── Keystore slot naming ────────────────────────────────────────────────────


def keystore_slot_oauth_client_id_for(bot_id: str) -> str:
    return f"zoom-oauth-client-id-{bot_id}"


def keystore_slot_oauth_client_secret_for(bot_id: str) -> str:
    return f"zoom-oauth-client-secret-{bot_id}"


def keystore_slot_oauth_redirect_url_for(bot_id: str) -> str:
    return f"zoom-oauth-redirect-url-{bot_id}"


def keystore_slot_credentials_dir_for(bot_id: str) -> str:
    return f"zoom-creds-dir-{bot_id}"


def keystore_slot_s2s_client_id_for(bot_id: str) -> str:
    return f"zoom-s2s-client-id-{bot_id}"


def keystore_slot_s2s_client_secret_for(bot_id: str) -> str:
    return f"zoom-s2s-client-secret-{bot_id}"


def keystore_slot_s2s_account_id_for(bot_id: str) -> str:
    return f"zoom-s2s-account-id-{bot_id}"


def credentials_dir_for(bot_home: str | Path) -> str:
    """The shim's per-bot credentials directory (under .openclaw)."""
    return str(Path(bot_home) / ".openclaw" / "zoom")


# ── Access panel ────────────────────────────────────────────────────────────


ZOOM_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": ZOOM_SKILL_ID,
    "skill_display_name": "Zoom",
    "summary": (
        "Lets this bot work with your Zoom account. Connect once for "
        "meeting search, recording lookup, and Zoom Docs access. "
        "Optionally connect a second time to let the bot create and send "
        "Zoom meeting links on your behalf."
    ),
    "will": [
        "Search your Zoom meetings by topic, date, or meeting number",
        "Look up recordings and meeting transcripts you have access to",
        "Read your Zoom Docs and My Notes",
        "Search your Zoom chat and meeting content",
        (
            "Create new Zoom Docs from Markdown (this is a write tool Zoom "
            "itself ships in their MCP)"
        ),
        (
            "Create new Zoom meetings and return the join link "
            "(if you set up meeting creation)"
        ),
        (
            "Reschedule or cancel meetings the bot created "
            "(if you set up meeting creation)"
        ),
    ],
    "wont": [
        "Join meetings, record audio, or listen in",
        "Read meetings you didn't attend or weren't invited to",
        (
            "Create meetings as other people in your Zoom account, "
            "unless you give it a specific person's email"
        ),
        "Send anyone the host-control link unless you specifically ask it to",
    ],
    "where_credentials_live": (
        "Connection details are stored only on this bot's user account on "
        "your machine. You can disconnect at any time from this page, and "
        "also from Zoom directly at zoom.us → Settings → Marketplace → "
        "Installed Apps."
    ),
}


# ── Install status ──────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Zoom install flow."""

    bot_id: str
    status: str
    user_oauth_email: Optional[str] = None
    s2s_account_id: Optional[str] = None
    mcp_server_present: bool = False
    has_user_oauth: bool = False
    has_s2s: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": ZOOM_SKILL_ID,
            "status": self.status,
            "user_oauth_email": self.user_oauth_email,
            "s2s_account_id": self.s2s_account_id,
            "mcp_server_present": self.mcp_server_present,
            "has_user_oauth": self.has_user_oauth,
            "has_s2s": self.has_s2s,
            "error": self.error,
        }


# ── Install plan ────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One UI step in the install plan."""

    id: str
    label: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "detail": self.detail}


def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """Map a status to the ordered list of remaining install steps."""
    if status.status == "active":
        return []
    if status.status == "read_only_configured":
        # Operator can stop here; offer optional S2S.
        return [
            InstallStep(
                id="set_s2s_credentials",
                label="Optional: enable meeting creation",
                detail=(
                    "Add a Server-to-Server OAuth app so the bot can create "
                    "Zoom meetings on your behalf."
                ),
            ),
        ]
    if status.status in ("fully_configured", "mcp_not_installed"):
        return [
            InstallStep(
                id="enable_in_oc_config",
                label="Wire it up",
                detail="Register the Zoom MCP server in this bot's config.",
            ),
        ]
    if status.status == "oauth_pending":
        return [
            InstallStep(
                id="complete_oauth",
                label="Finish Zoom sign-in",
                detail=(
                    "Open the authorize URL, sign in to the Zoom account "
                    "this bot should use, and paste the redirect code back."
                ),
            ),
            InstallStep(
                id="enable_in_oc_config",
                label="Wire it up",
            ),
        ]
    if status.status in ("oauth_expired", "mcp_unreachable", "s2s_invalid"):
        # Same recovery shape — re-do the failed step.
        return [
            InstallStep(
                id="complete_oauth",
                label="Re-authorize",
                detail="Your Zoom sign-in needs to be refreshed.",
            ),
        ]
    # not_configured (or unknown) — full flow.
    return [
        InstallStep(
            id="set_oauth_client",
            label="Connect a Zoom Marketplace app",
            detail=(
                "Paste your Zoom Marketplace OAuth app's Client ID, "
                "Client Secret, and Redirect URL."
            ),
        ),
        InstallStep(
            id="complete_oauth",
            label="Sign in to Zoom",
            detail=(
                "Open the authorize URL, sign in, and paste the redirect "
                "code back here."
            ),
        ),
        InstallStep(
            id="enable_in_oc_config",
            label="Wire it up",
        ),
    ]


# ── Type aliases for the closures the server wires in ───────────────────────


KeystoreReader = Callable[[str], Optional[str]]  # slot_name -> value or None
KeystoreWriter = Callable[[str, str], None]      # slot_name, value -> None
KeystoreDeleter = Callable[[str], bool]          # slot_name -> existed?
OcConfigReader = Callable[[str], Optional[dict]]  # bot_id -> openclaw.json dict
OcConfigWriter = Callable[[str, dict], None]      # bot_id, config -> None
BotHomeResolver = Callable[[str], str]            # bot_id -> /Users/<bot>
ShimRunner = Callable[
    [str, list[str], dict[str, str]], "ShimResult"
]  # bot_id, argv, env -> result


@dataclass
class ShimResult:
    """Outcome of a shim subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str


# ── Authorize URL construction ──────────────────────────────────────────────


def build_authorize_url(
    client_id: str,
    redirect_url: str,
    scopes: tuple[str, ...] = ZOOM_USER_OAUTH_SCOPES,
    state: Optional[str] = None,
) -> str:
    """Construct the Zoom authorize URL the operator opens in their browser.

    Pure function — used by the wizard to show the URL once the operator
    has supplied client_id + redirect_url. The shim has an equivalent
    helper; this one is here so the wizard can render the URL without
    spawning a subprocess.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_url,
        "scope": " ".join(scopes),
    }
    if state:
        params["state"] = state
    return f"https://zoom.us/oauth/authorize?{urllib.parse.urlencode(params)}"


# ── Credential setters ──────────────────────────────────────────────────────


def set_oauth_client(
    bot_id: str,
    client_id: str,
    client_secret: str,
    redirect_url: str,
    *,
    write_keystore: KeystoreWriter,
    bot_home_for: BotHomeResolver,
) -> tuple[bool, Optional[str]]:
    """Persist the General App OAuth credentials to the per-bot keystore.

    Does NOT validate by minting a token — Zoom only validates client_id
    + secret in combination with a real OAuth code, which we don't have
    until the operator completes the authorize step. Validation happens
    at ``complete_oauth`` time.

    Returns (ok, error). Errors:
      - "invalid_client_id" — empty or malformed.
      - "invalid_redirect_url" — empty or not a URL.
    """
    if not client_id.strip():
        return False, "invalid_client_id"
    if not client_secret.strip():
        return False, "invalid_client_secret"
    if not redirect_url.strip().lower().startswith(("https://", "http://")):
        return False, "invalid_redirect_url"
    write_keystore(keystore_slot_oauth_client_id_for(bot_id), client_id.strip())
    write_keystore(
        keystore_slot_oauth_client_secret_for(bot_id), client_secret.strip()
    )
    write_keystore(
        keystore_slot_oauth_redirect_url_for(bot_id), redirect_url.strip()
    )
    # Compute + store the credentials_dir path so other tools can read it
    # from the keystore the same way the launcher resolves env bindings.
    creds_dir = credentials_dir_for(bot_home_for(bot_id))
    write_keystore(keystore_slot_credentials_dir_for(bot_id), creds_dir)
    return True, None


def complete_oauth(
    bot_id: str,
    code: str,
    *,
    read_keystore: KeystoreReader,
    bot_home_for: BotHomeResolver,
    run_shim: ShimRunner,
) -> tuple[bool, Optional[str]]:
    """Exchange the OAuth code for tokens by invoking the shim's login.

    Shells out to ``evolve-zoom-mcp login --code <code>`` as the bot user
    (the ``run_shim`` closure handles the sudo + env plumbing). The shim
    persists the refresh token to ``<creds_dir>/credentials.json``.

    Returns (ok, error). Errors:
      - "oauth_client_missing" — set_oauth_client wasn't called first.
      - "invalid_code" — Zoom rejected the code (expired, single-use, etc.).
      - "shim_failed" — the shim subprocess failed for any other reason.
    """
    client_id = read_keystore(keystore_slot_oauth_client_id_for(bot_id))
    client_secret = read_keystore(keystore_slot_oauth_client_secret_for(bot_id))
    redirect_url = read_keystore(keystore_slot_oauth_redirect_url_for(bot_id))
    creds_dir = read_keystore(keystore_slot_credentials_dir_for(bot_id))
    if not (client_id and client_secret and redirect_url and creds_dir):
        return False, "oauth_client_missing"
    env = {
        "ZOOM_OAUTH_CLIENT_ID": client_id,
        "ZOOM_OAUTH_CLIENT_SECRET": client_secret,
        "ZOOM_OAUTH_REDIRECT_URL": redirect_url,
        "ZOOM_CREDENTIALS_DIR": creds_dir,
    }
    result = run_shim(bot_id, ["login", "--code", code], env)
    if result.returncode != 0:
        # The shim prints "[error] Zoom rejected the code: …" to stderr/stdout.
        text = (result.stderr or "") + " " + (result.stdout or "")
        if "rejected the code" in text or "invalid_grant" in text.lower():
            return False, "invalid_code"
        return False, "shim_failed"
    return True, None


def set_s2s_credentials(
    bot_id: str,
    client_id: str,
    client_secret: str,
    account_id: str,
    *,
    write_keystore: KeystoreWriter,
    mint_test_token: Optional[
        Callable[[str, str, str], tuple[bool, Optional[str]]]
    ] = None,
) -> tuple[bool, Optional[str]]:
    """Validate the S2S creds and persist to keystore.

    If ``mint_test_token`` is supplied, we call Zoom's token endpoint to
    validate the creds before persisting. Real production wiring always
    supplies it; tests can pass None to skip the network round-trip.

    Errors:
      - "invalid_client_credentials" — Zoom rejected the creds.
      - "missing_field" — any field is empty.
    """
    if not (client_id.strip() and client_secret.strip() and account_id.strip()):
        return False, "missing_field"
    if mint_test_token is not None:
        ok, err = mint_test_token(client_id.strip(), client_secret.strip(), account_id.strip())
        if not ok:
            return False, err or "invalid_client_credentials"
    write_keystore(keystore_slot_s2s_client_id_for(bot_id), client_id.strip())
    write_keystore(keystore_slot_s2s_client_secret_for(bot_id), client_secret.strip())
    write_keystore(keystore_slot_s2s_account_id_for(bot_id), account_id.strip())
    return True, None


# ── openclaw.json wiring ────────────────────────────────────────────────────


def _build_mcp_server_block(bot_id: str, has_s2s: bool) -> dict[str, Any]:
    """Construct the ``mcp.servers.zoom`` block to merge into openclaw.json."""
    env_bindings: dict[str, str] = {
        "ZOOM_OAUTH_CLIENT_ID": f"keystore:{keystore_slot_oauth_client_id_for(bot_id)}",
        "ZOOM_OAUTH_CLIENT_SECRET": f"keystore:{keystore_slot_oauth_client_secret_for(bot_id)}",
        "ZOOM_OAUTH_REDIRECT_URL": f"keystore:{keystore_slot_oauth_redirect_url_for(bot_id)}",
        "ZOOM_CREDENTIALS_DIR": f"keystore:{keystore_slot_credentials_dir_for(bot_id)}",
    }
    if has_s2s:
        env_bindings["ZOOM_S2S_CLIENT_ID"] = (
            f"keystore:{keystore_slot_s2s_client_id_for(bot_id)}"
        )
        env_bindings["ZOOM_S2S_CLIENT_SECRET"] = (
            f"keystore:{keystore_slot_s2s_client_secret_for(bot_id)}"
        )
        env_bindings["ZOOM_S2S_ACCOUNT_ID"] = (
            f"keystore:{keystore_slot_s2s_account_id_for(bot_id)}"
        )
    return {
        "transport": "stdio",
        "command": "uvx",
        "args": ["evolve-zoom-mcp"],
        "env_bindings": env_bindings,
    }


def enable_in_oc_config(
    bot_id: str,
    *,
    read_oc_config: OcConfigReader,
    write_oc_config: OcConfigWriter,
    read_keystore: KeystoreReader,
) -> tuple[bool, Optional[str]]:
    """Merge the ``mcp.servers.zoom`` block into the bot's openclaw.json.

    Idempotent: re-running after S2S is added simply layers the new
    env_bindings into the existing block (whose ``command`` and ``args``
    stay unchanged).
    """
    config = read_oc_config(bot_id)
    if config is None:
        return False, "oc_config_unreadable"
    has_s2s = bool(
        read_keystore(keystore_slot_s2s_client_id_for(bot_id))
        and read_keystore(keystore_slot_s2s_client_secret_for(bot_id))
        and read_keystore(keystore_slot_s2s_account_id_for(bot_id))
    )
    mcp = config.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    servers[ZOOM_SERVER_ID] = _build_mcp_server_block(bot_id, has_s2s=has_s2s)
    write_oc_config(bot_id, config)
    return True, None


# ── Status resolver ─────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_oc_config: OcConfigReader,
    read_keystore: KeystoreReader,
    bot_home_for: BotHomeResolver,
    probe_shim_tools_list: Optional[Callable[[str], bool]] = None,
) -> InstallStatus:
    """Compute the bot's current Zoom install state.

    See module docstring for the full state machine.

    The ``probe_shim_tools_list`` closure (optional in tests) spawns the
    shim and verifies ``tools/list`` returns ≥ 1 tool. When None, we treat
    presence of ``mcp.servers.zoom`` as the active signal and skip the
    live probe (status caps at ``read_only_configured`` / ``fully_configured``
    in that case).
    """
    cfg = read_oc_config(bot_id)
    if cfg is None:
        return InstallStatus(bot_id=bot_id, status="unknown", error="oc_read_failed")

    # Stage 1 — OAuth client keystore slots.
    has_oauth_client = bool(
        read_keystore(keystore_slot_oauth_client_id_for(bot_id))
        and read_keystore(keystore_slot_oauth_client_secret_for(bot_id))
        and read_keystore(keystore_slot_oauth_redirect_url_for(bot_id))
    )
    if not has_oauth_client:
        return InstallStatus(bot_id=bot_id, status="not_configured")

    # Stage 2 — credentials.json present on disk.
    creds_dir = read_keystore(keystore_slot_credentials_dir_for(bot_id)) or (
        credentials_dir_for(bot_home_for(bot_id))
    )
    creds_file = Path(creds_dir) / "credentials.json"
    has_user_oauth = creds_file.exists()
    if not has_user_oauth:
        return InstallStatus(
            bot_id=bot_id, status="oauth_pending", has_user_oauth=False
        )

    # Stage 3 — S2S optional.
    has_s2s = bool(
        read_keystore(keystore_slot_s2s_client_id_for(bot_id))
        and read_keystore(keystore_slot_s2s_client_secret_for(bot_id))
        and read_keystore(keystore_slot_s2s_account_id_for(bot_id))
    )
    s2s_account_id = read_keystore(keystore_slot_s2s_account_id_for(bot_id))

    # Stage 4 — mcp.servers.zoom present.
    mcp_servers = (cfg.get("mcp") or {}).get("servers") or {}
    mcp_server_present = ZOOM_SERVER_ID in mcp_servers
    if not mcp_server_present:
        return InstallStatus(
            bot_id=bot_id,
            status=(
                "fully_configured" if has_s2s else "mcp_not_installed"
            ),
            has_user_oauth=True,
            has_s2s=has_s2s,
            s2s_account_id=s2s_account_id,
        )

    # Stage 5 — live probe (optional).
    if probe_shim_tools_list is None:
        # No probe wired; declare the highest cred-supported state.
        return InstallStatus(
            bot_id=bot_id,
            status="active" if has_s2s else "read_only_configured",
            has_user_oauth=True,
            has_s2s=has_s2s,
            s2s_account_id=s2s_account_id,
            mcp_server_present=True,
            user_oauth_email=_read_user_email(creds_file),
        )
    alive = probe_shim_tools_list(bot_id)
    if not alive:
        return InstallStatus(
            bot_id=bot_id,
            status="mcp_unreachable",
            has_user_oauth=True,
            has_s2s=has_s2s,
            s2s_account_id=s2s_account_id,
            mcp_server_present=True,
            error="probe_failed",
        )
    return InstallStatus(
        bot_id=bot_id,
        status="active" if has_s2s else "read_only_configured",
        has_user_oauth=True,
        has_s2s=has_s2s,
        s2s_account_id=s2s_account_id,
        mcp_server_present=True,
        user_oauth_email=_read_user_email(creds_file),
    )


def _read_user_email(creds_file: Path) -> Optional[str]:
    """Best-effort read of user_email from credentials.json."""
    try:
        data = json.loads(creds_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    email = data.get("user_email")
    return str(email) if email else None


# ── Revoke ──────────────────────────────────────────────────────────────────


def revoke(
    bot_id: str,
    scope: str = "all",
    *,
    read_oc_config: OcConfigReader,
    write_oc_config: OcConfigWriter,
    delete_keystore: KeystoreDeleter,
    bot_home_for: BotHomeResolver,
    read_keystore: Optional[KeystoreReader] = None,
) -> tuple[bool, Optional[str]]:
    """Tear down some or all of the Zoom install.

    ``scope`` ∈ {"all", "read", "write"}:
      - "read":  delete user-OAuth keystore slots + credentials.json. If
                 ``scope=read`` leaves S2S still configured, the
                 ``mcp.servers.zoom`` entry stays but loses its OAuth
                 env_bindings (write tools still work after kickstart).
      - "write": delete S2S keystore slots only. The shim drops write
                 tools from ``tools/list`` automatically; we re-emit
                 the mcp block without S2S env_bindings.
      - "all":   both, plus remove ``mcp.servers.zoom`` from openclaw.json.

    Returns (ok, error).
    """
    if scope not in ("all", "read", "write"):
        return False, "invalid_scope"

    if scope in ("read", "all"):
        delete_keystore(keystore_slot_oauth_client_id_for(bot_id))
        delete_keystore(keystore_slot_oauth_client_secret_for(bot_id))
        delete_keystore(keystore_slot_oauth_redirect_url_for(bot_id))
        delete_keystore(keystore_slot_credentials_dir_for(bot_id))
        creds_file = Path(credentials_dir_for(bot_home_for(bot_id))) / "credentials.json"
        if creds_file.exists():
            try:
                creds_file.unlink()
            except OSError as exc:
                log.warning("zoom revoke: failed to delete %s: %s", creds_file, exc)

    if scope in ("write", "all"):
        delete_keystore(keystore_slot_s2s_client_id_for(bot_id))
        delete_keystore(keystore_slot_s2s_client_secret_for(bot_id))
        delete_keystore(keystore_slot_s2s_account_id_for(bot_id))

    config = read_oc_config(bot_id)
    if config is None:
        # Keystore was scrubbed; openclaw.json is unreachable. Return
        # success — the cred half is what mattered.
        return True, None
    mcp_servers = (config.get("mcp") or {}).get("servers") or {}
    if scope == "all":
        if ZOOM_SERVER_ID in mcp_servers:
            del mcp_servers[ZOOM_SERVER_ID]
            write_oc_config(bot_id, config)
    else:
        # Partial revoke — re-emit mcp.servers.zoom with the remaining halves.
        # If both halves are revoked we'd have hit the "all" branch.
        if ZOOM_SERVER_ID in mcp_servers and read_keystore is not None:
            has_s2s_left = bool(
                read_keystore(keystore_slot_s2s_client_id_for(bot_id))
            )
            has_oauth_left = bool(
                read_keystore(keystore_slot_oauth_client_id_for(bot_id))
            )
            if has_oauth_left or has_s2s_left:
                mcp_servers[ZOOM_SERVER_ID] = _build_mcp_server_block(
                    bot_id, has_s2s=has_s2s_left
                )
                write_oc_config(bot_id, config)
            else:
                del mcp_servers[ZOOM_SERVER_ID]
                write_oc_config(bot_id, config)
    return True, None


# ── Shim runner — production wiring ─────────────────────────────────────────


def run_shim_as_bot(
    bot_id: str,
    argv: list[str],
    env: dict[str, str],
    *,
    bot_home_for: BotHomeResolver,
) -> ShimResult:
    """Spawn ``evolve-zoom-mcp <argv>`` as the bot user.

    Production wiring: shells out via ``sudo -H -u <bot> uvx
    evolve-zoom-mcp <argv>`` from /tmp (per CLAUDE.md uv_cwd note). The
    env is passed through to the subprocess.

    Tests should pass a stub ``run_shim`` to ``complete_oauth`` etc.
    rather than calling this directly.
    """
    bot_home = bot_home_for(bot_id)
    # Run from /tmp to dodge the bot-user-can't-cd-into-pod-admin-user's-home gotcha.
    cmd = [
        "sudo",
        "-H",
        "-u",
        Path(bot_home).name,
    ]
    for key, value in env.items():
        cmd.append(f"{key}={value}")
    cmd.extend(["uvx", "evolve-zoom-mcp", *argv])
    proc = subprocess.run(
        cmd,
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=120,
    )
    return ShimResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# ── Registry entry (for /api/skills/catalog/<id>) ───────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": ZOOM_SKILL_ID,
    "display_name": "Zoom",
    "summary": ZOOM_ACCESS_PANEL["summary"],
    "access_panel": dict(ZOOM_ACCESS_PANEL),
}
