"""evolve_admin.skills.telegram_install — Telegram bot skill install flow.

Telegram as an installable skill lets any bot send messages to Telegram
chats, groups, or channels as a tool capability, independent of whether
Telegram is the bot's primary channel.

Token shape (BotFather, not OAuth)
------------------------------------
Telegram bots use a static ``Bot Token`` from BotFather — Telegram's
official bot-management bot. There is no OAuth dance: the operator creates
a bot via BotFather, copies the token, and pastes it into the install flow.

Key differences from Slack:

* **No OAuth dance.** Token is a static string from BotFather.
* **No scopes.** A Telegram bot's permissions are managed inside Telegram
  (you add the bot to groups/channels; it gets per-chat permission).
* **One token per Telegram bot, not per workspace.** Different shape from
  Slack's workspace-level install.

Token storage
-------------
Bot token plus basic bot metadata (username, first_name, can_join_groups,
can_read_all_group_messages) are stored at:
  ``<bot_home>/.openclaw/skills/telegram.json``
owned by the bot user. The evolve user writes via ``/tmp`` staging +
``sudo /bin/cp`` (per CLAUDE.md). The file is read via direct
``Path.read_text()`` with a ``sudo /bin/cat`` fallback.

Trust-chain decisions
---------------------
1. Token is stored per-bot in the bot's own ``~/.openclaw/`` directory,
   never centralised. Consistent with Slack's pattern.
2. Plain-language access panel — ``TELEGRAM_ACCESS_PANEL`` describes
   exactly what the bot will and won't do (Plex test).
3. Token verification is performed against Telegram's ``getMe`` API
   before storage. A 401 response means the token is invalid.
4. Remote revocation is not supported by Telegram's API (tokens are
   revoked via BotFather only). Local revoke removes the stored token.
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

#: Canonical id for the Telegram skill. Matches the manifest integration id.
TELEGRAM_SKILL_ID = "telegram"

#: Telegram token config file path (relative to bot home).
TELEGRAM_TOKEN_PATH = ".openclaw/skills/telegram.json"

#: Telegram API base URL.
TELEGRAM_API_BASE = "https://api.telegram.org"

#: HTTP timeout for Telegram API calls.
TELEGRAM_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

#: User-facing description of what Evolve will and will not do with the
#: Telegram bot access. Written for the Plex test (no "token", no API jargon,
#: every line a concrete user-recognisable action).
TELEGRAM_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": TELEGRAM_SKILL_ID,
    "skill_display_name": "Telegram",
    "summary": (
        "Lets this bot send messages to Telegram chats, groups, and channels "
        "it has been added to. Useful for notifications, alerts, and any "
        "workflow where you want the bot to reach you on Telegram."
    ),
    "will": [
        "Send messages to chats this bot has been added to",
        "Read messages in those chats (so it can respond)",
        "Edit and delete its own messages",
        "Send photos, documents, and formatted text",
    ],
    "wont": [
        "Join chats it hasn't been explicitly added to",
        "Read messages in chats where you haven't added it",
        "Access your private Telegram messages",
        "Share access with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The Telegram bot key is stored only on this bot's user account on "
        "your machine — never centralised, never sent off-pod. You can "
        "revoke access at any time from this page or by contacting "
        "@BotFather and revoking the bot's key directly."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Telegram install flow.

    State machine:

    * ``missing`` — bot has no Telegram key yet. Calling set-token starts
      the install.
    * ``valid`` — key present and Telegram's getMe returned ok. The bot
      can send to Telegram.
    * ``revoked`` — key present but Telegram rejected it (401 / not found).
      Must re-enter a new BotFather key.
    * ``invalid`` — key present but failed format validation before API check.
    * ``unknown`` — pre-flight read failed; UI surfaces the error string.
    """

    bot_id: str
    bot_token_state: str  # missing | valid | revoked | invalid | unknown
    bot_username: str | None = None
    bot_first_name: str | None = None
    can_join_groups: bool | None = None
    can_read_all_group_messages: bool | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        """Alias kept for orchestrator compatibility."""
        return self.bot_token_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": TELEGRAM_SKILL_ID,
            "status": self.bot_token_state,
            "bot_token_state": self.bot_token_state,
            "bot_username": self.bot_username,
            "bot_first_name": self.bot_first_name,
            "can_join_groups": self.can_join_groups,
            "can_read_all_group_messages": self.can_read_all_group_messages,
            "error": self.error,
        }


# ── Install steps ─────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI must drive to complete a Telegram install.

    Mirrors :class:`evolve_admin.skills.slack_install.InstallStep` exactly.
    """

    id: str  # set_token | confirm
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

    * ``missing`` → set_token step + confirmation.
    * ``revoked`` → same as missing (re-enter a new key).
    * ``invalid`` → same as missing (bad token format, re-enter).
    * ``valid`` → empty plan (nothing to do; UI shows success).
    * ``unknown`` → empty plan; UI surfaces the error.
    """
    if status.bot_token_state in ("valid", "unknown"):
        return []

    # missing, revoked, or invalid — enter/re-enter the BotFather key
    plan = [
        InstallStep(
            id="set_token",
            label="Enter your BotFather key",
            endpoint=f"/api/skills/install/{TELEGRAM_SKILL_ID}/set-token",
            payload={"bot_id": status.bot_id},
            access_panel=dict(TELEGRAM_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can send messages on Telegram",
            endpoint=f"/api/skills/install/{TELEGRAM_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]
    return plan


# ── Token validation ──────────────────────────────────────────────────────────

#: Telegram bot tokens match: <digits>:<alphanumeric+_->
#: (not enforced strictly — we let the API reject bad tokens with a clear error)
import re as _re
_TOKEN_PATTERN = _re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


def _token_looks_valid(token: str) -> bool:
    """Quick format check — does not guarantee the token works."""
    return bool(token and _TOKEN_PATTERN.match(token.strip()))


def verify_bot_token(token: str) -> dict:
    """Call Telegram's getMe API to verify a bot token.

    Returns a dict with keys:
    * ``ok`` (bool) — True if the token is valid.
    * ``bot_username`` (str | None)
    * ``bot_first_name`` (str | None)
    * ``can_join_groups`` (bool | None)
    * ``can_read_all_group_messages`` (bool | None)
    * ``error`` (str | None) — error description if not ok.
    * ``http_status`` (int) — the HTTP status code returned.

    Token format check is done first; Telegram API is only called for
    tokens that pass the basic format check.
    """
    token = (token or "").strip()

    if not token:
        return {
            "ok": False,
            "bot_username": None,
            "bot_first_name": None,
            "can_join_groups": None,
            "can_read_all_group_messages": None,
            "error": "empty_token",
            "http_status": 0,
        }

    if not _token_looks_valid(token):
        return {
            "ok": False,
            "bot_username": None,
            "bot_first_name": None,
            "can_join_groups": None,
            "can_read_all_group_messages": None,
            "error": "invalid_token_format",
            "http_status": 0,
        }

    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    status_code, body = _telegram_get_json(url)

    if status_code == 401 or (isinstance(body, dict) and not body.get("ok")):
        err = "unauthorized"
        if isinstance(body, dict):
            err = body.get("description") or "unauthorized"
        return {
            "ok": False,
            "bot_username": None,
            "bot_first_name": None,
            "can_join_groups": None,
            "can_read_all_group_messages": None,
            "error": err,
            "http_status": status_code,
        }

    if status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False,
            "bot_username": None,
            "bot_first_name": None,
            "can_join_groups": None,
            "can_read_all_group_messages": None,
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    result = body.get("result") or {}
    return {
        "ok": True,
        "bot_username": result.get("username"),
        "bot_first_name": result.get("first_name"),
        "can_join_groups": result.get("can_join_groups"),
        "can_read_all_group_messages": result.get("can_read_all_group_messages"),
        "error": None,
        "http_status": 200,
    }


def _telegram_get_json(url: str) -> tuple[int, dict | None]:
    """GET a Telegram API URL. Returns (status_code, parsed_json_or_None)."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except Exception as exc:
        log.debug("telegram_install: getMe network error: %s", exc)
        return 0, None


# ── Token storage ─────────────────────────────────────────────────────────────


def _token_path(bot_id: str) -> Path:
    """Return the path to the Telegram token config file for bot_id.

    Uses ``bot_home(bot_id)`` from config so the path is correct even when
    the bot's macOS username differs from bot_id. Never hardcodes /Users/{bot_id}.
    """
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / TELEGRAM_TOKEN_PATH


def read_token_config(bot_id: str) -> dict | None:
    """Read the Telegram token config for bot_id. Returns None if missing or unreadable.

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
    """Write the Telegram token config via /tmp staging + sudo /bin/cp.

    Returns (ok, error_message). The target directory is created if missing.
    Per CLAUDE.md: bot files are owned by the bot user; write via /tmp staging.
    """
    from ..config import bot_home as _bot_home, get_bot_user

    network = None  # bot_home loads lazily; get_bot_user needs network
    from ..config import load_network as _load_network
    network = _load_network()

    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / TELEGRAM_TOKEN_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-tg-", suffix=".json")
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
    """Delete the Telegram token config for bot_id. Returns True if cleared."""
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
# Writing skills/telegram.json alone does NOT make the Telegram channel plugin
# load on the bot's gateway. The gateway picks up channels from openclaw.json's
# ``channels.telegram`` block plus ``plugins.entries.telegram.enabled``. Without
# both keys (and a kickstart so the running process re-reads the file) inbound
# Telegram messages are silently dropped even though the credential is stored.
#
# Canonical shape, mirrored from a working bot's openclaw.json::
#
#   {
#     "channels": {
#       "telegram": {
#         "enabled": true,
#         "dmPolicy": "pairing",
#         "groupPolicy": "allowlist",
#         "botToken": "<token>",
#         "streaming": {"mode": "off"}
#       }
#     },
#     "plugins": {"entries": {"telegram": {"enabled": true}}}
#   }
#
# The ``botToken`` path is also the runtime mirror target for the auth-profiles
# registry (web/server.py::_RUNTIME_MIRROR_PATH); kept here so the install flow
# doesn't depend on the credential-rotate code path being reachable.
#
# Read/write/kickstart mechanics are shared with the other messaging skills
# via ``_oc_install_common`` so the install flow doesn't drift between
# providers. The channel block shape is owned here.

from . import _oc_install_common as _oc_common


#: Default channel-policy fields the gateway expects alongside ``botToken``.
#: Matches the shape produced by setup_wizard + deploy.py's drift-repair pass.
_DEFAULT_TELEGRAM_CHANNEL_FIELDS: dict[str, Any] = {
    "enabled": True,
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist",
    "streaming": {"mode": "off"},
}


# Re-exports kept so the existing route imports / tests keep working without
# tracking the file split.
_bot_oc_json_path = _oc_common.bot_oc_json_path
_read_oc_config = _oc_common.read_oc_config
kickstart_gateway = _oc_common.kickstart_gateway


def enable_channel_in_oc_config(
    bot_id: str,
    bot_token: str,
) -> tuple[bool, str | None]:
    """Merge ``channels.telegram`` + ``plugins.entries.telegram`` into the
    bot's openclaw.json and write it back.

    Idempotent — keys already present (e.g. operator-set ``dmPolicy``) are
    preserved. ``botToken`` and ``plugins.entries.telegram.enabled`` are
    always rewritten to the install-flow values.

    Returns (ok, error_message). On failure the openclaw.json is left
    unchanged on disk (atomic write via /tmp staging + ``sudo /bin/cp``).
    """
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    channels = cfg.setdefault("channels", {})
    tg = channels.setdefault("telegram", {})
    for key, default in _DEFAULT_TELEGRAM_CHANNEL_FIELDS.items():
        tg.setdefault(key, default)
    # Always overwrite enabled + botToken — the install flow is the source of
    # truth for both. Drop the legacy "token" key if a previous wizard left one.
    tg["enabled"] = True
    tg["botToken"] = bot_token
    tg.pop("token", None)

    entries = cfg.setdefault("plugins", {}).setdefault("entries", {})
    entries.setdefault("telegram", {})["enabled"] = True

    return _oc_common.write_oc_config(bot_id, cfg)


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_token: Callable[[str], "dict | None"] | None = None,
    check_token: Callable[[str], "dict"] | None = None,
) -> InstallStatus:
    """Resolve the bot's current Telegram install status.

    The reader callables are injectable for testability. In production the
    server wires the real helpers; tests inject dict/lambda stubs.

    Parameters
    ----------
    bot_id : str
        The bot to check.
    read_token : callable | None
        ``read_token(bot_id) → dict | None`` — reads the stored Telegram
        token config. Defaults to :func:`read_token_config`.
    check_token : callable | None
        ``check_token(token) → dict`` — validates the token against
        Telegram's getMe endpoint. Defaults to :func:`verify_bot_token`.
    """
    _read_token = read_token or read_token_config
    _check_token = check_token or verify_bot_token

    try:
        config = _read_token(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            bot_token_state="unknown",
            error=f"token_read_failed: {exc.__class__.__name__}: {exc}",
        )

    if not config or not config.get("bot_token"):
        return InstallStatus(
            bot_id=bot_id,
            bot_token_state="missing",
        )

    bot_token = config["bot_token"]

    # Basic format check before hitting the API
    if not _token_looks_valid(bot_token):
        return InstallStatus(
            bot_id=bot_id,
            bot_token_state="invalid",
            error="stored_token_format_invalid",
        )

    # Validate the token with Telegram
    try:
        result = _check_token(bot_token)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id,
            bot_token_state="unknown",
            bot_username=config.get("bot_username"),
            bot_first_name=config.get("bot_first_name"),
            error=f"getme_failed: {exc.__class__.__name__}: {exc}",
        )

    if not result.get("ok"):
        err = result.get("error") or "unauthorized"
        http_status = result.get("http_status", 0)
        # 401 = revoked/invalid; other errors = unknown
        if http_status == 401 or err == "unauthorized" or "Unauthorized" in err:
            return InstallStatus(
                bot_id=bot_id,
                bot_token_state="revoked",
                bot_username=config.get("bot_username"),
                bot_first_name=config.get("bot_first_name"),
                error=err,
            )
        return InstallStatus(
            bot_id=bot_id,
            bot_token_state="unknown",
            bot_username=config.get("bot_username"),
            bot_first_name=config.get("bot_first_name"),
            error=f"getme_error: {err}",
        )

    return InstallStatus(
        bot_id=bot_id,
        bot_token_state="valid",
        bot_username=result.get("bot_username"),
        bot_first_name=result.get("bot_first_name"),
        can_join_groups=result.get("can_join_groups"),
        can_read_all_group_messages=result.get("can_read_all_group_messages"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": TELEGRAM_SKILL_ID,
    "display_name": TELEGRAM_ACCESS_PANEL["skill_display_name"],
    "summary": TELEGRAM_ACCESS_PANEL["summary"],
    "access_panel": dict(TELEGRAM_ACCESS_PANEL),
}
