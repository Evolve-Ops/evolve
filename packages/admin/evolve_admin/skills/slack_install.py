"""evolve_admin.skills.slack_install — Slack workspace skill install flow.

Slack as an installable skill lets any bot send messages to Slack workspaces
and channels as a tool capability, independent of whether Slack is the bot's
primary channel. This is the first non-Google OAuth provider in Evolve and
validates the V2.1-1 oauth/ substrate with a real second provider.

OAuth shape (Slack v2, workspace-level)
---------------------------------------
Slack OAuth is workspace-scoped, not user-scoped. The resulting token grants
access to one Slack workspace. Key differences from Google:

* One token per workspace — multi-workspace support needs separate installs
  per workspace.
* Bot tokens (``xoxb-...``) vs user tokens (``xoxp-...``). Bot tokens are
  the right default: they send as the bot user, which is what a skill needs.
* Scopes are fine-grained. We request the minimum set needed for the
  "send messages + see channels + DM" use case:
    - ``chat:write`` — post messages as the bot
    - ``channels:read`` — list public channels
    - ``im:read`` — list DMs
    - ``im:write`` — open DMs
  A user who needs more (``files:write``, ``reactions:write``, etc.) can
  install additional scopes through the Slack app settings.

Token storage
-------------
Bot token (``xoxb-...``) plus workspace metadata are stored at:
  ``<bot_home>/.openclaw/skills/slack.json``
owned by the bot user. The evolve user writes via ``/tmp`` staging +
``sudo /bin/cp`` (per CLAUDE.md). The file is read via direct
``Path.read_text()`` with a ``sudo /bin/cat`` fallback.

Credentials
-----------
App-level credentials (Client ID + Client Secret) are pod-level, not per-bot.
They live in the shared keystore at:
  ``/Users/Shared/evolve/keystore/slack-client-id``
  ``/Users/Shared/evolve/keystore/slack-client-secret``
If missing, install returns ``credentials_missing`` and the UI surfaces a
clear error pointing to ``docs/skills/slack-setup.md``.

Trust-chain decisions
---------------------
1. Bot tokens only — we never request or store user tokens. User tokens have
   broader personal access; bot tokens are scoped to the app's workspace
   permissions, which the pod admin controls by setting scopes in the Slack
   app dashboard.
2. Per-bot storage — tokens are stored in the bot's own ``~/.openclaw/``
   directory, never centralized. Consistent with GOG's pattern.
3. Plain-language access panel — ``SLACK_ACCESS_PANEL`` describes exactly what
   the bot will and won't do with the granted access (Plex test).
4. Scopes are conservative by default — ``chat:write`` + ``channels:read`` +
   ``im:read`` + ``im:write``. No admin-level scopes.
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

#: Canonical id for the Slack skill. Matches the manifest integration id.
SLACK_SKILL_ID = "slack"

#: Default bot token scopes requested on a fresh install. Conservative — only
#: what's needed for "send + see channels + DM". No admin-level scopes.
SLACK_DEFAULT_SCOPES = (
    "chat:write",
    "channels:read",
    "im:read",
    "im:write",
)

#: Keystore credential file names (relative to shared_dir/keystore/).
SLACK_KEYSTORE_CLIENT_ID = "slack-client-id"
SLACK_KEYSTORE_CLIENT_SECRET = "slack-client-secret"

#: Slack token config file path (relative to bot home).
SLACK_TOKEN_PATH = ".openclaw/skills/slack.json"

#: Slack OAuth endpoints.
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"

#: State token TTL in seconds (10 minutes, same as Google).
SLACK_OAUTH_STATE_TTL_S = 600

#: HTTP timeout for Slack API calls.
SLACK_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

#: User-facing description of what Evolve will and will not do with the
#: Slack workspace access. Written for the Plex test (no "scopes", no
#: OAuth jargon, every line a concrete user-recognisable action).
SLACK_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": SLACK_SKILL_ID,
    "skill_display_name": "Slack",
    "summary": (
        "Lets this bot send messages to your Slack workspace — to channels "
        "or as direct messages. Useful for notifications, alerts, and "
        "any workflow where you want the bot to reach you (or your team) "
        "on Slack."
    ),
    "will": [
        "Send messages to public and private channels it has been invited to",
        "Send direct messages to workspace members",
        "See the list of channels in your workspace",
        "See the list of direct message conversations",
    ],
    "wont": [
        "Read message history or DM contents",
        "Delete or edit messages written by others",
        "Invite itself to channels without your action",
        "Access files, calls, or admin settings",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The Slack bot token is stored only on this bot's user account on "
        "your machine — never centralised, never sent off-pod. You can "
        "revoke access at any time from this page or from your Slack app "
        "settings at api.slack.com/apps."
    ),
    "default_scopes": list(SLACK_DEFAULT_SCOPES),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Slack install flow.

    State machine:

    * ``credentials_missing`` — pod-level Slack app credentials (Client ID /
      Client Secret) are not configured. Pod admin must register a Slack app
      and add credentials to the keystore (see docs/skills/slack-setup.md).
      Pre-empts every per-bot install.
    * ``missing`` — credentials present but bot has no Slack token yet.
      Calling install kicks off the OAuth flow.
    * ``valid`` — token present and the workspace is reachable (auth.test
      passed). The bot can send to Slack.
    * ``revoked`` — token present but the workspace rejected it (auth.test
      returned ``invalid_auth`` or ``token_revoked``). Must re-authorise.
    * ``unknown`` — pre-flight read failed; UI surfaces the error string.
    """

    bot_id: str
    token_state: str  # credentials_missing | missing | valid | revoked | unknown
    workspace_id: str | None = None
    workspace_name: str | None = None
    bot_user_id: str | None = None
    scopes_granted: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        """Alias kept for orchestrator compatibility (mirrors GOG convention)."""
        return self.token_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": SLACK_SKILL_ID,
            "status": self.token_state,
            "token_state": self.token_state,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "bot_user_id": self.bot_user_id,
            "scopes_granted": list(self.scopes_granted),
            "error": self.error,
        }


# ── Install steps ─────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI must drive to complete a Slack install.

    Mirrors :class:`evolve_admin.skills.gog_install.InstallStep` exactly.
    """

    id: str  # configure_credentials | oauth | confirm
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

    * ``credentials_missing`` → one step: pod admin registers a Slack app.
    * ``missing`` → OAuth step + confirmation.
    * ``revoked`` → same as missing (re-authorise).
    * ``valid`` → empty plan (nothing to do; UI shows success).
    * ``unknown`` → empty plan; UI surfaces the error.
    """
    if status.token_state == "credentials_missing":
        return [
            InstallStep(
                id="configure_credentials",
                label="Register a Slack app for this pod",
                endpoint=None,
                payload={"hint": (
                    "Pod admin needs to register a Slack app and add credentials "
                    "to the keystore. See docs/skills/slack-setup.md."
                )},
            )
        ]

    if status.token_state in ("valid", "unknown"):
        return []

    # missing or revoked — run the OAuth flow
    plan = [
        InstallStep(
            id="oauth",
            label="Connect to your Slack workspace",
            endpoint=f"/api/skills/install/{SLACK_SKILL_ID}/start-oauth",
            payload={"bot_id": status.bot_id},
            access_panel=dict(SLACK_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can send messages to Slack",
            endpoint=f"/api/skills/install/{SLACK_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]

    return plan


# ── Credential helpers ────────────────────────────────────────────────────────


def read_slack_credentials(shared_dir: Path) -> tuple[str | None, str | None]:
    """Read pod-level Slack app credentials from the keystore.

    Returns (client_id, client_secret). Either may be None if missing or
    unreadable. The keystore files are owned by the ``evolve`` user (direct
    write, no sudo needed under the shared dir).
    """
    keystore_dir = shared_dir / "keystore"
    client_id: str | None = None
    client_secret: str | None = None

    id_path = keystore_dir / SLACK_KEYSTORE_CLIENT_ID
    secret_path = keystore_dir / SLACK_KEYSTORE_CLIENT_SECRET

    try:
        if id_path.exists():
            client_id = id_path.read_text(encoding="utf-8").strip() or None
    except Exception as exc:
        log.warning("slack_install: could not read client-id: %s", exc)

    try:
        if secret_path.exists():
            client_secret = secret_path.read_text(encoding="utf-8").strip() or None
    except Exception as exc:
        log.warning("slack_install: could not read client-secret: %s", exc)

    return client_id, client_secret


# ── Token storage ─────────────────────────────────────────────────────────────


def _token_path(bot_id: str) -> Path:
    """Return the path to the Slack token config file for bot_id.

    Uses ``bot_home(bot_id)`` from config so the path is correct even when
    the bot's macOS username differs from bot_id. Never hardcodes /Users/{bot_id}.
    """
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / SLACK_TOKEN_PATH


def read_token_config(bot_id: str) -> dict | None:
    """Read the Slack token config for bot_id. Returns None if missing or unreadable.

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
    """Write the Slack token config via /tmp staging + sudo /bin/cp.

    Returns (ok, error_message). The target directory is created if missing.
    Per CLAUDE.md: bot files are owned by the bot user; write via /tmp staging.
    """
    from ..config import bot_home as _bot_home, get_bot_user

    user = get_bot_user(bot_id)
    home = _bot_home(bot_id)
    dest = str(home / SLACK_TOKEN_PATH)
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
    """Delete the Slack token config for bot_id. Returns True if cleared."""
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
# Writing skills/slack.json alone does NOT make the Slack channel plugin load
# on the bot's gateway — same shape as the Telegram bug (#1757). Without
# ``channels.slack`` + ``plugins.entries.slack.enabled = true`` (and a
# kickstart so the running process re-reads openclaw.json) inbound Slack
# messages are silently dropped.
#
# Canonical shape, mirrored from a working bot (team_bot_a)::
#
#   {
#     "channels": {
#       "slack": {
#         "enabled": true,
#         "mode": "socket",
#         "botToken": "xoxb-…",
#         "dmPolicy": "pairing",
#         "groupPolicy": "allowlist",
#         "streaming": {"mode": "off", "nativeTransport": false},
#         "userTokenReadOnly": true
#       }
#     },
#     "plugins": {"entries": {"slack": {"enabled": true}}}
#   }
#
# Slack's "socket" mode also requires an App-Level Token (``xapp-…``), which
# the OAuth flow does NOT deliver — only the bot token (``xoxb-…``) comes
# back from the OAuth callback. The App-Level Token is set out-of-band via
# the Slack admin UI / CLI. We still wire the channel up with the OAuth
# token because:
#
#   * The bot token + workspace metadata get safely preserved across deploys.
#   * The plugin entry is enabled so the gateway loads it on next boot —
#     making downstream "missing appToken" diagnostics observable.
#   * Operator-set fields (``mode``, ``allowFrom``, channel allowlist, …)
#     survive a redo of the install. Idempotent.
#
# Read/write/kickstart mechanics shared with ``telegram_install`` via
# ``_oc_install_common``.

from . import _oc_install_common as _oc_common


#: Default channel-policy fields the gateway expects alongside ``botToken``.
#: Matches the shape produced by setup_wizard + drift-repair passes. ``mode``
#: is intentionally NOT defaulted here — sockets require ``appToken`` which we
#: don't have from OAuth, and webhook mode requires ``webhookPath``. The
#: operator picks the mode; the install only seeds the safe-default policy
#: fields and the credential.
_DEFAULT_SLACK_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist",
    "streaming": {"mode": "off"},
    "userTokenReadOnly": True,
}


# Re-exports kept so callers can ``from slack_install import kickstart_gateway``
# without tracking the file split.
kickstart_gateway = _oc_common.kickstart_gateway


def enable_channel_in_oc_config(
    bot_id: str,
    bot_token: str,
) -> tuple[bool, str | None]:
    """Merge ``channels.slack`` + ``plugins.entries.slack`` into the bot's
    openclaw.json and write it back.

    Idempotent — operator-set fields like ``mode`` / ``appToken`` /
    ``allowFrom`` survive a redo of the install. ``botToken`` and
    ``plugins.entries.slack.enabled`` are always rewritten to the
    install-flow values.

    Returns ``(ok, error_message)``. On failure the openclaw.json is left
    unchanged on disk (atomic write via /tmp staging + ``sudo /bin/cp``).
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    sl = channels.setdefault("slack", {})
    for key, default in _DEFAULT_SLACK_CHANNEL_FIELDS.items():
        sl.setdefault(key, default)
    sl["enabled"] = True
    sl["botToken"] = bot_token
    # Drop the legacy "token" key if a previous wizard left one.
    sl.pop("token", None)

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("slack", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    shared_dir: Path | None = None,
    read_token: Callable[[str], "dict | None"] | None = None,
    check_workspace: Callable[[str], "tuple[bool, str | None]"] | None = None,
) -> InstallStatus:
    """Resolve the bot's current Slack install status.

    The reader callables are injectable for testability. In production the
    server wires the real helpers; tests inject dict/lambda stubs.

    Parameters
    ----------
    bot_id : str
        The bot to check.
    shared_dir : Path | None
        Shared directory for reading pod-level credentials. If None and
        ``check_workspace`` is None, credential check is skipped (caller
        supplies their own ``check_workspace``).
    read_token : callable | None
        ``read_token(bot_id) → dict | None`` — reads the stored Slack
        token config. Defaults to :func:`read_token_config`.
    check_workspace : callable | None
        ``check_workspace(bot_token) → (ok, error)`` — validates the token
        against Slack's auth.test endpoint. Defaults to
        :func:`_slack_auth_test_live`.
    """
    _read_token = read_token or read_token_config

    # Check pod-level credentials first
    if shared_dir is not None:
        client_id, client_secret = read_slack_credentials(shared_dir)
        if not client_id or not client_secret:
            return InstallStatus(
                bot_id=bot_id,
                token_state="credentials_missing",
                error=(
                    "Pod admin needs to register a Slack app and add credentials "
                    "to the keystore. See docs/skills/slack-setup.md."
                ),
            )
    # If shared_dir is None, caller manages credential checks externally
    # (typically test code that injects check_workspace directly).

    try:
        config = _read_token(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            token_state="unknown",
            error=f"token_read_failed: {exc.__class__.__name__}: {exc}",
        )

    if not config or not config.get("bot_token"):
        return InstallStatus(
            bot_id=bot_id,
            token_state="missing",
        )

    bot_token = config["bot_token"]
    workspace_id = config.get("workspace_id")
    workspace_name = config.get("workspace_name")
    bot_user_id = config.get("bot_user_id")
    scopes_granted = list(config.get("scopes", []))

    # Validate the token with Slack if checker is available
    _check = check_workspace or _slack_auth_test_live
    try:
        ok, err = _check(bot_token)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            token_state="unknown",
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            bot_user_id=bot_user_id,
            scopes_granted=scopes_granted,
            error=f"auth_test_failed: {exc.__class__.__name__}: {exc}",
        )

    if not ok:
        return InstallStatus(
            bot_id=bot_id,
            token_state="revoked",
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            bot_user_id=bot_user_id,
            scopes_granted=scopes_granted,
            error=err,
        )

    return InstallStatus(
        bot_id=bot_id,
        token_state="valid",
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        bot_user_id=bot_user_id,
        scopes_granted=scopes_granted,
    )


# ── Live Slack API helpers ────────────────────────────────────────────────────


def _slack_http_post_json(url: str, fields: dict) -> tuple[int, dict | None]:
    """POST application/x-www-form-urlencoded to a Slack endpoint.
    Returns (status, parsed_json).
    """
    import urllib.request
    import urllib.error
    import urllib.parse

    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=SLACK_HTTP_TIMEOUT_S) as resp:
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


def _slack_auth_test_live(bot_token: str) -> tuple[bool, str | None]:
    """Call Slack's auth.test endpoint to validate a bot token.

    Returns (True, None) if valid; (False, error_string) otherwise.
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(SLACK_AUTH_TEST_URL)
    req.add_header("Authorization", f"Bearer {bot_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=SLACK_HTTP_TIMEOUT_S) as resp:
            import json as _json
            raw = resp.read().decode("utf-8", errors="replace")
            body = _json.loads(raw) if raw else {}
            if body.get("ok"):
                return True, None
            return False, body.get("error") or "auth_test_failed"
    except urllib.error.HTTPError as e:
        return False, f"http_error_{e.code}"
    except Exception as exc:
        return False, f"network_error: {exc}"


def exchange_code_for_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> tuple[bool, dict | None, str | None]:
    """Exchange an OAuth authorization code for a workspace bot token.

    Calls Slack's ``oauth.v2.access`` endpoint.

    Returns (ok, token_response_dict, error_string).
    On success: ``token_response_dict`` contains ``access_token``,
    ``team.id``, ``team.name``, ``bot_user_id``, ``scope``, etc.
    """
    status, body = _slack_http_post_json(SLACK_TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    })

    if status != 200 or not isinstance(body, dict):
        return False, body, f"token_exchange_http_{status}"

    if not body.get("ok"):
        return False, body, body.get("error") or "token_exchange_failed"

    return True, body, None


# ── State-token store (in-memory, per-server-process) ──────────────────────────
# Mirrors Google's pattern. State is short-lived (10 min) and not persisted.

import threading as _threading
import time as _time

_SLACK_OAUTH_STATE: dict[str, dict] = {}
_SLACK_OAUTH_STATE_LOCK = _threading.Lock()


def slack_state_create(bot_id: str, redirect_uri: str) -> str:
    """Create a fresh OAuth state token for a Slack install flow."""
    import secrets
    state = secrets.token_urlsafe(24)
    expires_at = _time.time() + SLACK_OAUTH_STATE_TTL_S
    with _SLACK_OAUTH_STATE_LOCK:
        _SLACK_OAUTH_STATE[state] = {
            "bot_id": bot_id,
            "redirect_uri": redirect_uri,
            "expires_at": expires_at,
            "result": {"status": "pending"},
        }
        # Opportunistic GC
        now = _time.time()
        for k in [k for k, v in _SLACK_OAUTH_STATE.items() if v.get("expires_at", 0) < now]:
            del _SLACK_OAUTH_STATE[k]
    return state


def slack_state_get(state: str) -> dict | None:
    """Look up a state entry. Returns None for unknown or expired state."""
    with _SLACK_OAUTH_STATE_LOCK:
        entry = _SLACK_OAUTH_STATE.get(state)
        if not entry:
            return None
        if entry.get("expires_at", 0) < _time.time():
            del _SLACK_OAUTH_STATE[state]
            return None
        return dict(entry)


def slack_state_set_result(state: str, result: dict) -> bool:
    """Set the result on a state entry (called by callback)."""
    with _SLACK_OAUTH_STATE_LOCK:
        entry = _SLACK_OAUTH_STATE.get(state)
        if not entry or entry.get("expires_at", 0) < _time.time():
            return False
        entry["result"] = result
        return True


def slack_state_consume(state: str) -> dict | None:
    """Pop and return a state entry (after success/error, to prevent replay)."""
    with _SLACK_OAUTH_STATE_LOCK:
        return _SLACK_OAUTH_STATE.pop(state, None)


def validate_callback(state: str, code: str) -> tuple[bool, str | None]:
    """Validate an OAuth callback's state token. Returns (ok, error).

    Checks that the state is known and not expired. Does NOT consume it —
    the route layer consumes after successful token exchange.
    """
    if not state:
        return False, "missing_state"
    if not code:
        return False, "missing_code"
    entry = slack_state_get(state)
    if not entry:
        return False, "unknown_or_expired_state"
    return True, None


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": SLACK_SKILL_ID,
    "display_name": SLACK_ACCESS_PANEL["skill_display_name"],
    "summary": SLACK_ACCESS_PANEL["summary"],
    "default_scopes": list(SLACK_DEFAULT_SCOPES),
    "access_panel": dict(SLACK_ACCESS_PANEL),
}
