"""evolve_admin.skills.discord_install — Discord server skill install flow.

Discord as an installable skill lets any bot send messages to Discord servers
(guilds) and channels as a tool capability, independent of whether Discord is
the bot's primary channel. Mirrors the Slack skill pattern (V2.1-2) since
Discord is server/workspace-shaped like Slack.

OAuth shape (Discord OAuth2, bot-level)
---------------------------------------
Discord OAuth is application-level, not per-guild. The bot token grants access
to all guilds the bot has been invited to. Key differences from Slack:

* One bot token per Discord application — the token is global across guilds.
* Guild membership requires a separate invite step: the pod admin generates a
  bot invite URL and adds the bot to each Discord server they want it to join.
* Scopes: ``bot`` (required for all bot actions) + ``applications.commands``
  (slash commands). We do NOT request ``messages.read`` at the application
  level because reading messages in guilds requires message_content intent
  (a privileged intent requiring Discord verification for large bots). We
  default to send-only unless the bot opts in to message content.
* The returned ``access_token`` from the OAuth flow is NOT the bot token.
  After exchanging the code, we use the ``access_token`` to identify the
  authorizing user; the actual bot token (``Bot <bot_token>``) comes from
  the Discord Developer Portal and is stored separately in the keystore.

Token storage
-------------
Bot token plus application metadata are stored at:
  ``<bot_home>/.openclaw/skills/discord.json``
owned by the bot user. The evolve user writes via ``/tmp`` staging +
``sudo /bin/cp`` (per CLAUDE.md). The file is read via direct
``Path.read_text()`` with a ``sudo /bin/cat`` fallback.

Credentials
-----------
App-level credentials (Client ID + Client Secret + Bot Token) are pod-level.
They live in the shared keystore at:
  ``/Users/Shared/evolve/keystore/discord-client-id``
  ``/Users/Shared/evolve/keystore/discord-client-secret``
  ``/Users/Shared/evolve/keystore/discord-bot-token``
If Client ID or Client Secret are missing, install returns
``credentials_missing`` and the UI surfaces a clear error pointing to
``docs/skills/discord-setup.md``.

The bot token is required for the bot to actually post messages. It is also
stored here because it is a pod-level credential (one Discord application per
pod, shared across all bots on the pod that want Discord access).

Discord OAuth vs. Slack OAuth differences
-----------------------------------------
Slack OAuth returns a workspace-scoped bot token per workspace. Discord works
differently:

1. The OAuth authorization flow (code → token) authenticates the *operator*
   and can be used to add the bot to guilds via the ``bot`` scope. The
   ``access_token`` from this flow is a user token, not a bot token.

2. The actual bot token lives in the Discord Developer Portal under the Bot
   section. It is set once during app creation and does not change with each
   OAuth flow.

3. Our install flow therefore has two steps:
   a. Operator creates the Discord app, copies the bot token from the portal,
      and adds it to the keystore (one-time setup). See docs/skills/discord-setup.md.
   b. Operator uses the generated invite URL to add the bot to the desired
      guild(s). This is a Discord authorization URL, not an OAuth token exchange.

This is structurally simpler than Slack: there is no per-install code → token
exchange. The bot token is credential-based, not user-authorization-based.
The OAuth flow is used only to generate the guild-invite URL.

Trust-chain decisions
---------------------
1. Bot token only — we never request or store user tokens. The bot token is
   scoped to the application's permissions set in the Developer Portal.
2. Per-bot storage — tokens are stored in the bot's own ``~/.openclaw/``
   directory, never centralized. Consistent with Slack's pattern.
3. Plain-language access panel — ``DISCORD_ACCESS_PANEL`` describes exactly
   what the bot will and won't do (Plex test).
4. Conservative default permissions bitmask — send messages + read channel
   history + view channels + use slash commands. No administrator permission.
5. No privileged intents by default — we don't enable Server Members Intent
   or Message Content Intent, which require Discord verification for bots in
   100+ servers. These can be enabled manually in the Discord Developer Portal.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Discord skill. Matches the manifest integration id.
DISCORD_SKILL_ID = "discord"

#: OAuth scopes requested on the guild-invite authorization URL.
#: ``bot`` is required for the bot to join and act in guilds.
#: ``applications.commands`` enables slash commands.
DISCORD_DEFAULT_SCOPES = (
    "bot",
    "applications.commands",
)

#: Default permissions bitmask for the bot invite URL.
#: 274877908992 = VIEW_CHANNEL (1024) | SEND_MESSAGES (2048) |
#:                READ_MESSAGE_HISTORY (65536) | USE_APPLICATION_COMMANDS (2147483648)
#:
#: Bit breakdown:
#:   0x00000400 = 1024        — VIEW_CHANNEL
#:   0x00000800 = 2048        — SEND_MESSAGES
#:   0x00010000 = 65536       — READ_MESSAGE_HISTORY
#:   0x80000000 = 2147483648  — USE_APPLICATION_COMMANDS
#: Sum: 1024 + 2048 + 65536 + 2147483648 = 2149550256
DISCORD_DEFAULT_PERMISSIONS = 2149550256

#: Keystore credential file names (relative to shared_dir/keystore/).
DISCORD_KEYSTORE_CLIENT_ID = "discord-client-id"
DISCORD_KEYSTORE_CLIENT_SECRET = "discord-client-secret"
DISCORD_KEYSTORE_BOT_TOKEN = "discord-bot-token"

#: Discord token config file path (relative to bot home).
DISCORD_TOKEN_PATH = ".openclaw/skills/discord.json"

#: Discord OAuth / API endpoints.
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_CURRENT_USER_URL = "https://discord.com/api/v10/users/@me"

#: State token TTL in seconds (10 minutes, same as Slack/Google).
DISCORD_OAUTH_STATE_TTL_S = 600

#: HTTP timeout for Discord API calls.
DISCORD_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

#: User-facing description of what Evolve will and will not do with the
#: Discord server access. Written for the Plex test (no "scopes", no
#: OAuth jargon, every line a concrete user-recognisable action).
DISCORD_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": DISCORD_SKILL_ID,
    "skill_display_name": "Discord",
    "summary": (
        "Lets this bot send messages to your Discord server — to channels "
        "it has been invited to. Useful for notifications, alerts, and any "
        "workflow where you want the bot to reach you (or your team) on Discord."
    ),
    "will": [
        "Send messages in channels this bot has been added to",
        "See the list of channels in servers it has joined",
        "Use slash commands you create",
    ],
    "wont": [
        "Join servers it hasn't been explicitly invited to",
        "Read direct messages between other users",
        "Access channels where the bot isn't a member",
        "Read message content in channels (requires Discord verification for that feature)",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The Discord bot token is stored only on this bot's user account on "
        "your machine — never centralised, never sent off-pod. You can "
        "revoke access at any time from this page or from the Discord "
        "Developer Portal at discord.com/developers/applications."
    ),
    "default_scopes": list(DISCORD_DEFAULT_SCOPES),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Discord install flow.

    State machine:

    * ``credentials_missing`` — pod-level Discord app credentials (Client ID /
      Client Secret / Bot Token) are not configured. Pod admin must register a
      Discord app and add credentials to the keystore. Pre-empts every per-bot
      install. See docs/skills/discord-setup.md.
    * ``missing`` — credentials present but bot has no Discord config yet.
      Next step is to invite the bot to a guild using the generated invite URL.
    * ``valid`` — bot token present and the Discord API responds correctly
      (GET /users/@me passed). The bot can send to Discord.
    * ``revoked`` — bot token present but Discord rejected it. Must update the
      bot token in the keystore.
    * ``unknown`` — pre-flight read failed; UI surfaces the error string.
    """

    bot_id: str
    token_state: str  # credentials_missing | missing | valid | revoked | unknown
    bot_user_id: str | None = None
    bot_username: str | None = None
    invite_url: str | None = None  # rendered with client_id + permissions + scopes
    invited_guilds: list[str] | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        """Alias kept for orchestrator compatibility (mirrors GOG/Slack convention)."""
        return self.token_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": DISCORD_SKILL_ID,
            "status": self.token_state,
            "token_state": self.token_state,
            "bot_user_id": self.bot_user_id,
            "bot_username": self.bot_username,
            "invite_url": self.invite_url,
            "invited_guilds": list(self.invited_guilds) if self.invited_guilds is not None else None,
            "error": self.error,
        }


# ── Install steps ─────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI must drive to complete a Discord install.

    Mirrors :class:`evolve_admin.skills.slack_install.InstallStep` exactly.
    """

    id: str  # configure_credentials | invite | confirm
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

    * ``credentials_missing`` → one step: pod admin registers a Discord app.
    * ``missing`` → invite step (generate guild invite URL) + confirmation.
    * ``revoked`` → same as missing (bot token needs updating in keystore).
    * ``valid`` → empty plan (nothing to do; UI shows success).
    * ``unknown`` → empty plan; UI surfaces the error.
    """
    if status.token_state == "credentials_missing":
        return [
            InstallStep(
                id="configure_credentials",
                label="Register a Discord app for this pod",
                endpoint=None,
                payload={"hint": (
                    "Pod admin needs to register a Discord app and add credentials "
                    "to the keystore. See docs/skills/discord-setup.md."
                )},
            )
        ]

    if status.token_state in ("valid", "unknown"):
        return []

    # missing or revoked — invite bot to guild + confirm
    invite_url = status.invite_url or ""
    plan = [
        InstallStep(
            id="invite",
            label="Invite the bot to your Discord server",
            endpoint=f"/api/skills/install/{DISCORD_SKILL_ID}/start-oauth",
            payload={"bot_id": status.bot_id, "invite_url": invite_url},
            access_panel=dict(DISCORD_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can send messages to Discord",
            endpoint=f"/api/skills/install/{DISCORD_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]

    return plan


# ── Credential helpers ────────────────────────────────────────────────────────


def read_discord_credentials(shared_dir: Path) -> tuple[str | None, str | None, str | None]:
    """Read pod-level Discord app credentials from the keystore.

    Returns (client_id, client_secret, bot_token). Any may be None if missing
    or unreadable. The keystore files are owned by the ``evolve`` user.
    """
    keystore_dir = shared_dir / "keystore"
    client_id: str | None = None
    client_secret: str | None = None
    bot_token: str | None = None

    for name, path in [
        ("client_id", keystore_dir / DISCORD_KEYSTORE_CLIENT_ID),
        ("client_secret", keystore_dir / DISCORD_KEYSTORE_CLIENT_SECRET),
        ("bot_token", keystore_dir / DISCORD_KEYSTORE_BOT_TOKEN),
    ]:
        try:
            if path.exists():
                val = path.read_text(encoding="utf-8").strip() or None
                if name == "client_id":
                    client_id = val
                elif name == "client_secret":
                    client_secret = val
                else:
                    bot_token = val
        except Exception as exc:
            log.warning("discord_install: could not read %s: %s", name, exc)

    return client_id, client_secret, bot_token


# ── Invite URL builder ────────────────────────────────────────────────────────


def build_invite_url(
    client_id: str,
    permissions: int = DISCORD_DEFAULT_PERMISSIONS,
    scopes: tuple[str, ...] = DISCORD_DEFAULT_SCOPES,
) -> str:
    """Build the Discord bot invite URL for a given application.

    The invite URL directs the user to Discord's guild-selection screen.
    After the user selects a server and confirms, the bot is added to that
    guild. No code → token exchange is needed: the bot token is already
    in the keystore.

    Parameters
    ----------
    client_id : str
        The Discord application client ID.
    permissions : int
        Permissions bitmask for the bot. Defaults to
        ``DISCORD_DEFAULT_PERMISSIONS`` (send + view + read history + slash commands).
    scopes : tuple[str, ...]
        OAuth scopes. Defaults to ``DISCORD_DEFAULT_SCOPES`` (``bot`` +
        ``applications.commands``).
    """
    import urllib.parse as _up
    params = {
        "client_id": client_id,
        "permissions": str(permissions),
        "scope": " ".join(scopes),
    }
    return f"{DISCORD_AUTHORIZE_URL}?{_up.urlencode(params)}"


# ── Token storage ─────────────────────────────────────────────────────────────


def _token_path(bot_id: str) -> Path:
    """Return the path to the Discord token config file for bot_id.

    Uses ``bot_home(bot_id)`` from config so the path is correct even when
    the bot's macOS username differs from bot_id. Never hardcodes /Users/{bot_id}.
    """
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / DISCORD_TOKEN_PATH


def read_token_config(bot_id: str) -> dict | None:
    """Read the Discord token config for bot_id. Returns None if missing or unreadable.

    Direct read first (ACL set by set_evolve_read_acl during deploy), then
    sudo /bin/cat fallback per CLAUDE.md.
    """
    p = _token_path(bot_id)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except PermissionError:
        pass
    except Exception:
        return None

    # Fallback: sudo /bin/cat (pre-ACL-setup bots)
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass

    return None


def write_token_config(bot_id: str, config: dict) -> tuple[bool, str | None]:
    """Write the Discord token config via /tmp staging + sudo /bin/cp.

    Returns (ok, error_message). The target directory is created if missing.
    Per CLAUDE.md: bot files are owned by the bot user; write via /tmp staging.
    """
    from ..config import bot_home as _bot_home, get_bot_user

    user = get_bot_user(bot_id)
    home = _bot_home(bot_id)
    dest = str(home / DISCORD_TOKEN_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)

        # Ensure skills/ directory exists
        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", skills_dir],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"mkdir failed: {r.stderr.strip() or 'unknown'}"

        # Fix ownership of skills/ dir to bot user
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", skills_dir],
            capture_output=True, text=True, timeout=10,
        )

        # Copy config file
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, dest],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"cp failed: {r.stderr.strip() or 'unknown'}"

        # Fix ownership of config file to bot user
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", dest],
            capture_output=True, text=True, timeout=10,
        )

        return True, None
    except Exception as exc:
        return False, f"write_config_error: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def delete_token_config(bot_id: str) -> bool:
    """Delete the Discord token config for bot_id. Returns True if cleared."""
    p = _token_path(bot_id)
    try:
        if p.exists():
            p.unlink()
            return True
        # Try via sudo in case we lack write ACL
        r = subprocess.run(
            ["sudo", "/bin/rm", "-f", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── openclaw.json wiring + gateway restart ────────────────────────────────────
#
# Writing skills/discord.json alone does NOT make the Discord channel plugin
# load on the bot's gateway — same shape as the Telegram bug (#1757) and the
# Slack fix. Without ``channels.discord`` + ``plugins.entries.discord.enabled``
# (and a kickstart so the running process re-reads openclaw.json) inbound
# Discord messages are silently dropped even though the credential is on disk.
#
# Canonical shape, mirrored from a working Discord-attached bot::
#
#   {
#     "channels": {
#       "discord": {
#         "enabled": true,
#         "dmPolicy": "pairing",
#         "groupPolicy": "allowlist",
#         "botToken": "<token>",
#         "streaming": {"mode": "off"}
#       }
#     },
#     "plugins": {"entries": {"discord": {"enabled": true}}}
#   }
#
# Read/write/kickstart mechanics shared with ``telegram_install`` /
# ``slack_install`` via ``_oc_install_common``.

from . import _oc_install_common as _oc_common


#: Default channel-policy fields the gateway expects alongside ``botToken``.
#: Matches the shape produced by the setup_wizard's Discord branch and the
#: existing Discord-attached bot configuration on the reference deployment.
_DEFAULT_DISCORD_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist",
    "streaming": {"mode": "off"},
}


# Re-export kept so the install route can ``from discord_install import
# kickstart_gateway`` without tracking the file split.
kickstart_gateway = _oc_common.kickstart_gateway


def enable_channel_in_oc_config(
    bot_id: str,
    bot_token: str,
) -> tuple[bool, str | None]:
    """Merge ``channels.discord`` + ``plugins.entries.discord`` into the
    bot's openclaw.json and write it back.

    Idempotent — operator-set policy fields (``dmPolicy``, ``groupPolicy``,
    allowlists) survive a redo of the install. ``botToken`` and
    ``plugins.entries.discord.enabled`` are always rewritten to the
    install-flow values.

    Returns ``(ok, error_message)``. On failure the openclaw.json is left
    unchanged on disk (atomic write via /tmp staging + ``sudo /bin/cp``).
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    dc = channels.setdefault("discord", {})
    for key, default in _DEFAULT_DISCORD_CHANNEL_FIELDS.items():
        dc.setdefault(key, default)
    # Always overwrite enabled + token — the install flow is the source of
    # truth for both. OC's bundled discord plugin reads
    # ``channels.discord.token`` (verified: 31x .token vs 0x botToken in
    # /opt/homebrew/lib/node_modules/openclaw/dist/discord-*.js, and
    # team_bot_b's live working config on the mini uses ``token``). Drop the
    # legacy "botToken" key if a previous wizard write left one — failing
    # to do so leaves a dead key that confuses status-correctness drift
    # checks. See internal/skills-deep-audit-2026-05-30.md P0-3 for the
    # incident this guards against (running the install on team_bot_b with the
    # original ``botToken`` write would have deleted team_bot_b's working
    # ``token`` and replaced it with a key OC ignores).
    dc["enabled"] = True
    dc["token"] = bot_token
    dc.pop("botToken", None)

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("discord", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    shared_dir: Path | None = None,
    read_token: Callable[[str], "dict | None"] | None = None,
    check_token: Callable[[str], "tuple[bool, str | None, dict | None]"] | None = None,
) -> InstallStatus:
    """Resolve the bot's current Discord install status.

    The reader callables are injectable for testability. In production the
    server wires the real helpers; tests inject dict/lambda stubs.

    Parameters
    ----------
    bot_id : str
        The bot to check.
    shared_dir : Path | None
        Shared directory for reading pod-level credentials. If None,
        credential check is skipped (caller manages it externally).
    read_token : callable | None
        ``read_token(bot_id) → dict | None`` — reads the stored Discord
        token config. Defaults to :func:`read_token_config`.
    check_token : callable | None
        ``check_token(bot_token) → (ok, error, user_info_dict)`` — validates
        the bot token against Discord's /users/@me endpoint. Defaults to
        :func:`_discord_verify_token_live`.
    """
    _read_token = read_token or read_token_config

    # Check pod-level credentials first
    client_id: str | None = None
    bot_token_from_keystore: str | None = None

    if shared_dir is not None:
        client_id, _client_secret, bot_token_from_keystore = read_discord_credentials(shared_dir)
        if not client_id or not bot_token_from_keystore:
            return InstallStatus(
                bot_id=bot_id,
                token_state="credentials_missing",
                error=(
                    "Pod admin needs to register a Discord app and add credentials "
                    "to the keystore. See docs/skills/discord-setup.md."
                ),
            )

    # Build invite URL for display (only possible when we have client_id)
    invite_url: str | None = None
    if client_id:
        invite_url = build_invite_url(client_id)

    # Try to read the per-bot config (records that the bot has been set up)
    try:
        config = _read_token(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            token_state="unknown",
            invite_url=invite_url,
            error=f"token_read_failed: {exc.__class__.__name__}: {exc}",
        )

    # Use the bot token from keystore (pod-level); per-bot config just records
    # that this bot has been activated and stores guild metadata.
    effective_token = bot_token_from_keystore or (config or {}).get("bot_token")

    if not config or not effective_token:
        # No per-bot config yet — bot hasn't been set up on this bot.
        # Missing means: credentials exist but bot hasn't been configured.
        return InstallStatus(
            bot_id=bot_id,
            token_state="missing",
            invite_url=invite_url,
        )

    bot_user_id = config.get("bot_user_id")
    bot_username = config.get("bot_username")
    invited_guilds = config.get("invited_guilds")

    # Validate the token with Discord if checker is available
    _check = check_token or _discord_verify_token_live
    try:
        ok, err, user_info = _check(effective_token)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            token_state="unknown",
            bot_user_id=bot_user_id,
            bot_username=bot_username,
            invite_url=invite_url,
            invited_guilds=invited_guilds,
            error=f"token_check_failed: {exc.__class__.__name__}: {exc}",
        )

    if not ok:
        return InstallStatus(
            bot_id=bot_id,
            token_state="revoked",
            bot_user_id=bot_user_id,
            bot_username=bot_username,
            invite_url=invite_url,
            invited_guilds=invited_guilds,
            error=err,
        )

    # Update from live user info if available
    if user_info:
        bot_user_id = bot_user_id or user_info.get("id")
        bot_username = bot_username or user_info.get("username")

    return InstallStatus(
        bot_id=bot_id,
        token_state="valid",
        bot_user_id=bot_user_id,
        bot_username=bot_username,
        invite_url=invite_url,
        invited_guilds=invited_guilds,
    )


# ── Live Discord API helpers ──────────────────────────────────────────────────


def _discord_http_get_json(
    url: str,
    bot_token: str,
) -> tuple[int, dict | None]:
    """GET a Discord API endpoint with Bot token auth.
    Returns (status_code, parsed_json).
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bot {bot_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=DISCORD_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                import json as _json
                return resp.status, _json.loads(raw) if raw else {}
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            import json as _json
            return e.code, _json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _discord_verify_token_live(bot_token: str) -> tuple[bool, str | None, dict | None]:
    """Call Discord's /users/@me endpoint to validate a bot token.

    Returns (True, None, user_info_dict) if valid;
            (False, error_string, None) otherwise.

    The /users/@me endpoint with Bot auth returns information about the bot
    application, which is sufficient to confirm the token is valid.
    """
    status, body = _discord_http_get_json(DISCORD_CURRENT_USER_URL, bot_token)

    if status == 200 and isinstance(body, dict) and body.get("id"):
        return True, None, body

    if status == 401:
        return False, "invalid_token", None
    if status == 403:
        return False, "forbidden", None
    if status == 0:
        return False, "network_error", None

    error_msg = (body or {}).get("message") or f"http_{status}"
    return False, error_msg, None


def verify_token(bot_token: str) -> tuple[bool, str | None, dict | None]:
    """Public wrapper around _discord_verify_token_live for use by routes.

    Returns (ok, error, user_info). The user_info dict contains ``id``,
    ``username``, and other fields from the Discord /users/@me response.
    """
    return _discord_verify_token_live(bot_token)


# ── State-token store (in-memory, per-server-process) ──────────────────────────
# Mirrors Slack's pattern. State is short-lived (10 min) and not persisted.
# Used for the guild-invite flow (we generate a state token so the UI can
# detect when the user has completed the invite flow in another tab).

import threading as _threading
import time as _time

_DISCORD_OAUTH_STATE: dict[str, dict] = {}
_DISCORD_OAUTH_STATE_LOCK = _threading.Lock()


def discord_state_create(bot_id: str, invite_url: str) -> str:
    """Create a fresh state token for a Discord guild-invite flow."""
    import secrets
    state = secrets.token_urlsafe(24)
    expires_at = _time.time() + DISCORD_OAUTH_STATE_TTL_S
    with _DISCORD_OAUTH_STATE_LOCK:
        _DISCORD_OAUTH_STATE[state] = {
            "bot_id": bot_id,
            "invite_url": invite_url,
            "expires_at": expires_at,
            "result": {"status": "pending"},
        }
        # Opportunistic GC
        now = _time.time()
        for k in [k for k, v in _DISCORD_OAUTH_STATE.items() if v.get("expires_at", 0) < now]:
            del _DISCORD_OAUTH_STATE[k]
    return state


def discord_state_get(state: str) -> dict | None:
    """Look up a state entry. Returns None for unknown or expired state."""
    with _DISCORD_OAUTH_STATE_LOCK:
        entry = _DISCORD_OAUTH_STATE.get(state)
        if not entry:
            return None
        if entry.get("expires_at", 0) < _time.time():
            del _DISCORD_OAUTH_STATE[state]
            return None
        return dict(entry)


def discord_state_set_result(state: str, result: dict) -> bool:
    """Set the result on a state entry (called after user completes invite)."""
    with _DISCORD_OAUTH_STATE_LOCK:
        entry = _DISCORD_OAUTH_STATE.get(state)
        if not entry or entry.get("expires_at", 0) < _time.time():
            return False
        entry["result"] = result
        return True


def discord_state_consume(state: str) -> dict | None:
    """Pop and return a state entry (after success/error, to prevent replay)."""
    with _DISCORD_OAUTH_STATE_LOCK:
        return _DISCORD_OAUTH_STATE.pop(state, None)


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": DISCORD_SKILL_ID,
    "display_name": DISCORD_ACCESS_PANEL["skill_display_name"],
    "summary": DISCORD_ACCESS_PANEL["summary"],
    "default_scopes": list(DISCORD_DEFAULT_SCOPES),
    "access_panel": dict(DISCORD_ACCESS_PANEL),
}
