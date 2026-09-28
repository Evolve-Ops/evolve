"""evolve_admin.skills.home_assistant_install — Home Assistant skill install flow.

.. note::
   **Withdrawn from catalog 2026-05-30.** The server routes and inventory
   detection have been removed because no code consumed the credential
   file at runtime — install completed but bots couldn't actually use
   Home Assistant. The module stays for :func:`verify_token`'s HA REST
   API check, reusable when this skill returns as an MCP-server install
   (e.g. ``homeassistant-mcp``). See
   ``internal/design/paste-token-skills-future-2026-05-30.md``.

Home Assistant is the universal abstraction over smart-home devices for the
"someone running Plex and Home Assistant" audience (per Evolve's Plex-test
design constraint). Adding it as an installable skill lets any bot read
device state, query sensors, and trigger automations through HA's REST API.

Auth shape (long-lived access token, not OAuth)
------------------------------------------------
HA does not ship an OAuth app registration flow useful for headless bots.
The canonical pattern is: user opens their HA profile page, generates a
Long-Lived Access Token, and pastes it into the install flow. The token
is a static string that authenticates against ``GET <base_url>/api/``
with the ``Authorization: Bearer ...`` header.

Two pieces of config are needed, not one:

* **Base URL** — where the user's HA instance is reachable from the pod
  (e.g. ``http://homeassistant.local:8123`` or ``http://192.168.x.x:8123``).
* **Access token** — the LLAT pasted by the user.

This is the only meaningful structural difference from the Telegram
installer — that flow has a single string to verify; HA needs URL + token.

Token storage
-------------
URL + token are stored at ``<bot_home>/.openclaw/skills/home_assistant.json``
owned by the bot user. Reads use direct ``Path.read_text()`` with a
``sudo /bin/cat`` fallback (per CLAUDE.md). Writes go through ``/tmp``
staging + ``sudo /bin/cp``. The token is never centralised; nothing leaves
this bot's user account on the mini.

Network reachability
--------------------
HA installs are typically on the same LAN as the pod (Home Assistant Yellow,
HAOS box on Pi, or HAOS VM on a Mac/NAS). The install flow does a real HTTP
call from the admin process to validate; if the URL is unreachable we
report the failure with a Plex-test-friendly message rather than letting
the user paste a token only to discover the URL is wrong an hour later.

Local revocation
----------------
HA's LLAT system has no remote-revoke API exposed for bots — tokens are
revoked via the HA UI (Profile → Long-Lived Access Tokens → trash icon).
``delete_token_config`` removes our locally stored token; the user can
still revoke it in HA itself for defence in depth.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical id for the Home Assistant skill. Matches the manifest integration id.
HOME_ASSISTANT_SKILL_ID = "home_assistant"

#: Config file path relative to bot home.
HOME_ASSISTANT_CONFIG_PATH = ".openclaw/skills/home_assistant.json"

#: HTTP timeout for HA API verification calls.
HOME_ASSISTANT_HTTP_TIMEOUT_S = 8


# ── Plain-language access panel ───────────────────────────────────────────────

HOME_ASSISTANT_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": HOME_ASSISTANT_SKILL_ID,
    "skill_display_name": "Home Assistant",
    "summary": (
        "Lets this bot read what's happening in your home — temperature, "
        "lights, locks, presence — and trigger automations you've already "
        "set up in Home Assistant."
    ),
    "will": [
        "Read the state of devices you've added to Home Assistant",
        "Trigger automations and scripts you have already created",
        "Check sensor values (temperature, humidity, motion, etc.)",
        "Tell you which lights are on or who is home",
    ],
    "wont": [
        "Add or remove devices in Home Assistant",
        "Change automations or scripts you haven't asked it to",
        "Send any home data outside this bot's machine",
        "Access Home Assistant accounts other than the one this token is from",
    ],
    "where_credentials_live": (
        "Your Home Assistant access key is stored only on this bot's user "
        "account on your machine — never centralised, never sent off-pod. "
        "Revoke at any time from your Home Assistant profile page."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of a bot's Home Assistant install state.

    State machine:

    * ``missing``  — no config saved yet; user hasn't entered URL+token.
    * ``valid``    — config present and Home Assistant accepted the token.
                     The bot can read state and call services.
    * ``revoked``  — config present but HA returned 401 (token revoked or
                     replaced). User must paste a fresh token.
    * ``unreachable`` — config present but the base URL doesn't respond
                     (HA is down, wrong URL, network unreachable).
    * ``invalid``  — config present but URL or token failed format checks
                     before we hit HA. User must fix the config.
    * ``unknown``  — pre-flight read failed; ``error`` has the detail.
    """

    bot_id: str
    status: str  # missing | valid | revoked | unreachable | invalid | unknown
    base_url: str | None = None
    ha_version: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": HOME_ASSISTANT_SKILL_ID,
            "status": self.status,
            "base_url": self.base_url,
            "ha_version": self.ha_version,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives to complete the install.

    Mirrors the shape of other installers (Telegram, Obsidian) plus a
    ``fields`` block describing what the credential-form step collects."""

    id: str  # set_config | confirm
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
    """Return the steps remaining for *status*.

    * ``valid``        — empty plan; UI shows already-installed.
    * ``unknown``      — empty plan; UI surfaces the error.
    * everything else  — set_config step (URL + token form) + confirm.
    """
    if status.status in ("valid", "unknown"):
        return []

    return [
        InstallStep(
            id="set_config",
            label="Enter your Home Assistant address and access key",
            endpoint=f"/api/skills/install/{HOME_ASSISTANT_SKILL_ID}/set-config",
            payload={"bot_id": status.bot_id},
            fields=[
                {
                    "name": "base_url",
                    "label": "Home Assistant address",
                    "placeholder": "http://homeassistant.local:8123",
                    "type": "url",
                    "help": (
                        "Where you reach your Home Assistant from this network. "
                        "Most installs work at http://homeassistant.local:8123."
                    ),
                },
                {
                    "name": "access_token",
                    "label": "Long-lived access key",
                    "placeholder": "Paste the key from your Home Assistant profile",
                    "type": "password",
                    "help": (
                        "Open Home Assistant → click your profile (lower left) "
                        "→ scroll to Long-Lived Access Tokens → Create Token. "
                        "Copy and paste it here."
                    ),
                },
            ],
            access_panel=dict(HOME_ASSISTANT_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm this bot can talk to Home Assistant",
            endpoint=f"/api/skills/install/{HOME_ASSISTANT_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── Config validation ─────────────────────────────────────────────────────────

#: HA URLs are http(s)://host[:port]. We reject anything else as a guardrail
#: against pastes like file:// or javascript: that would be confusing or
#: dangerous if rendered as a link.
_URL_PATTERN = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _url_looks_valid(base_url: str) -> bool:
    """Lightweight URL validity check — does not guarantee reachability."""
    if not base_url:
        return False
    if not _URL_PATTERN.match(base_url.strip()):
        return False
    try:
        parsed = urlparse(base_url.strip())
        return bool(parsed.scheme in ("http", "https") and parsed.netloc)
    except Exception:
        return False


def _token_looks_valid(token: str) -> bool:
    """LLATs are JWT-shaped strings ≥100 chars. Plenty conservative."""
    return bool(token and len(token.strip()) >= 32)


def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes so we can always append /api/* safely."""
    return base_url.rstrip("/")


# ── Token verification ────────────────────────────────────────────────────────


def verify_token(base_url: str, token: str) -> dict:
    """Call ``GET <base_url>/api/`` and check the bearer token works.

    Returns a dict with:
    * ``ok`` (bool)              — True if HA accepted the token.
    * ``status`` (str)           — valid | revoked | unreachable | invalid.
    * ``ha_version`` (str|None)  — HA version reported by /api/config.
    * ``error`` (str|None)
    * ``http_status`` (int)
    """
    base_url = (base_url or "").strip()
    token = (token or "").strip()

    if not _url_looks_valid(base_url):
        return {
            "ok": False, "status": "invalid",
            "ha_version": None,
            "error": "invalid_url_format",
            "http_status": 0,
        }
    if not _token_looks_valid(token):
        return {
            "ok": False, "status": "invalid",
            "ha_version": None,
            "error": "invalid_token_format",
            "http_status": 0,
        }

    url = f"{_normalize_base_url(base_url)}/api/"
    status_code, body, err = _ha_get_json(url, token)

    if err == "connection_failed":
        return {
            "ok": False, "status": "unreachable",
            "ha_version": None,
            "error": "connection_failed",
            "http_status": 0,
        }

    if status_code == 401:
        return {
            "ok": False, "status": "revoked",
            "ha_version": None,
            "error": "unauthorized",
            "http_status": 401,
        }

    if status_code != 200:
        return {
            "ok": False, "status": "unreachable",
            "ha_version": None,
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    # /api/ returns {"message": "API running."} on healthy HA. Try to pull
    # version from /api/config (cheap follow-up; non-fatal if it fails).
    ha_version = None
    try:
        cfg_status, cfg_body, _ = _ha_get_json(
            f"{_normalize_base_url(base_url)}/api/config", token,
        )
        if cfg_status == 200 and isinstance(cfg_body, dict):
            ha_version = cfg_body.get("version")
    except Exception:
        pass

    return {
        "ok": True, "status": "valid",
        "ha_version": ha_version,
        "error": None,
        "http_status": 200,
    }


def _ha_get_json(url: str, token: str) -> tuple[int, dict | None, str | None]:
    """GET an HA API URL with bearer auth. Returns (status, body, err)."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HOME_ASSISTANT_HTTP_TIMEOUT_S) as resp:
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
    except Exception as exc:
        log.debug("home_assistant_install: GET %s failed: %s", url, exc)
        return 0, None, "connection_failed"


# ── Config storage ────────────────────────────────────────────────────────────


def _config_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / HOME_ASSISTANT_CONFIG_PATH


def read_config(bot_id: str) -> dict | None:
    """Read the saved HA config for *bot_id*. None when missing or unreadable."""
    p = _config_path(bot_id)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except PermissionError:
        pass
    except Exception:
        return None

    # Pre-ACL fallback (same pattern as the other skills).
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


def write_config(bot_id: str, config: dict) -> tuple[bool, str | None]:
    """Write the HA config via /tmp staging + sudo /bin/cp."""
    from ..config import bot_home as _bot_home, get_bot_user, load_network as _load_network

    network = _load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / HOME_ASSISTANT_CONFIG_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-ha-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)

        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", skills_dir],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"mkdir failed: {r.stderr.strip() or 'unknown'}"

        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", skills_dir],
            capture_output=True, text=True, timeout=10,
        )

        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, dest],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"cp failed: {r.stderr.strip() or 'unknown'}"

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


def delete_config(bot_id: str) -> bool:
    """Delete the saved config for *bot_id*. Returns True on clear."""
    p = _config_path(bot_id)
    try:
        if p.exists():
            p.unlink()
            return True
        r = subprocess.run(
            ["sudo", "/bin/rm", "-f", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Status resolver ───────────────────────────────────────────────────────────


def resolve_status(
    bot_id: str,
    *,
    read_cfg: Callable[[str], "dict | None"] | None = None,
    check_token: Callable[[str, str], "dict"] | None = None,
) -> InstallStatus:
    """Resolve the bot's current HA install status.

    Injectable callables match the Telegram pattern so tests can stub the
    filesystem read and HTTP verification independently.
    """
    _read = read_cfg or read_config
    _check = check_token or verify_token

    try:
        cfg = _read(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"config_read_failed: {exc.__class__.__name__}: {exc}",
        )

    if not cfg or not cfg.get("base_url") or not cfg.get("access_token"):
        return InstallStatus(bot_id=bot_id, status="missing")

    base_url = cfg["base_url"]
    token = cfg["access_token"]

    if not _url_looks_valid(base_url) or not _token_looks_valid(token):
        return InstallStatus(
            bot_id=bot_id, status="invalid",
            base_url=base_url,
            error="stored_config_format_invalid",
        )

    try:
        result = _check(base_url, token)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            base_url=base_url,
            error=f"verify_failed: {exc.__class__.__name__}: {exc}",
        )

    if result.get("ok"):
        return InstallStatus(
            bot_id=bot_id, status="valid",
            base_url=base_url,
            ha_version=result.get("ha_version"),
        )

    return InstallStatus(
        bot_id=bot_id, status=result.get("status") or "unknown",
        base_url=base_url,
        error=result.get("error"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": HOME_ASSISTANT_SKILL_ID,
    "display_name": HOME_ASSISTANT_ACCESS_PANEL["skill_display_name"],
    "summary": HOME_ASSISTANT_ACCESS_PANEL["summary"],
    "access_panel": dict(HOME_ASSISTANT_ACCESS_PANEL),
}
