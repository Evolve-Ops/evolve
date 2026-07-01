"""evolve_admin.skills.notion_install — Notion skill install flow.

.. note::
   **Rewired 2026-05-30 as the third MCP-backed install** — third worked
   example of the pattern Obsidian (#1817) and Dropbox (#1819) established,
   and the FIRST that's not filesystem-backed. Notion installs the vetted
   ``@notionhq/notion-mcp-server`` (first-party from Notion) and stores
   the credential in the pod keystore rather than a sidecar JSON file.
   See ``docs/design/skills-install-roadmap-2026-05-30.md`` for the
   roadmap and ``project_obsidian_mcp_install_pattern`` (memory) for the
   reference shape. The differences from Obsidian/Dropbox:

     * **API-key skill, not filesystem.** Scope is governed by Notion's
       per-page sharing model (operator shares pages with the integration
       in Notion's UI), not by an OS-level ACL. No read/read+write mode
       toggle — the integration either has access to a page or doesn't.
     * **Credentials in the pod keystore**, not on the bot's filesystem.
       The keystore slot is ``notion-<bot>`` (per-bot, mirroring Telegram's
       per-bot token model). InstallMcpServer's ``env_bindings`` references
       the slot; the launcher resolves it at exec time.
     * **JSON-encoded headers blob.** ``@notionhq/notion-mcp-server`` reads
       ``OPENAPI_MCP_HEADERS`` — a JSON string containing the Bearer auth
       AND the Notion-Version header. The wrapper route hides this from
       the operator: they paste a plain Internal Integration Secret;
       :func:`build_headers_json` constructs the JSON and saves it.

Notion is the household + service-business knowledge surface — Carla's
client decision logs, Diana's foundation board notes, Marcus's matter
files. Adding it as an installable skill lets any bot read pages and
databases the user has shared with their integration, search Notion,
and append notes when asked.

Auth shape (internal integration token, not OAuth)
---------------------------------------------------
Notion has two auth flows: a full OAuth app (requires registering an OAuth
app + dealing with redirect URIs) and an "internal integration token"
(user creates an Integration in their workspace and gets a static secret).
For the Plex-test audience, internal integration is the right default:

  1. User opens https://www.notion.so/profile/integrations
  2. Creates a new Internal Integration scoped to their workspace
  3. Copies the Internal Integration Secret (starts with ``secret_`` or
     ``ntn_`` depending on token vintage)
  4. Shares the specific Notion pages/databases they want the bot to see
     with that integration (Notion-side: Share → invite the integration)
  5. Pastes the secret into our install flow

This module verifies the token against ``GET /v1/users/me`` and stores
``{access_token, bot_id, workspace_name?, integration_name?}`` at
``<bot_home>/.openclaw/skills/notion.json``.

Token vintages
--------------
Both ``secret_`` (legacy) and ``ntn_`` (May 2024+) tokens authenticate the
same way. We accept either format. Internal-integration tokens are revoked
by deleting the integration in Notion — there is no remote revoke API
exposed for token holders, so revocation is local-only on our side.
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

#: Canonical id for the Notion skill.
NOTION_SKILL_ID = "notion"

#: Config file path relative to bot home.
NOTION_CONFIG_PATH = ".openclaw/skills/notion.json"

#: Notion API base URL.
NOTION_API_BASE = "https://api.notion.com/v1"

#: Notion-Version header value. Notion pins API behaviour per version.
NOTION_API_VERSION = "2022-06-28"

#: HTTP timeout.
NOTION_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

NOTION_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": NOTION_SKILL_ID,
    "skill_display_name": "Notion",
    "summary": (
        "Lets this bot read and edit pages and databases in your Notion "
        "workspace. Useful for pulling notes into briefings, querying "
        "databases, and saving bot-generated summaries as new pages. "
        "Scope is governed by which pages you share with the integration "
        "in Notion — pages you don't explicitly share remain invisible."
    ),
    "will": [
        "Read pages and databases you have shared with the integration",
        "Search across pages the integration can see",
        "Create new pages and append blocks to existing ones (when asked)",
        "Query databases by filters and sort order",
    ],
    "wont": [
        "Read pages you have not shared with the integration "
        "(Notion enforces this — the bot literally cannot see them)",
        "Change pages or databases you haven't asked it to",
        "Share your Notion content with anyone outside this bot",
        "Sign in to your Notion account on your behalf",
    ],
    "where_credentials_live": (
        "Your Internal Integration Secret is stored only in this pod's "
        "keystore — never centralised, never sent off-pod. To revoke "
        "access, open Notion → My integrations → delete the integration "
        "(or unshare individual pages). Local revoke (via this UI) "
        "removes the keystore slot but the integration object remains "
        "in your Notion workspace until you delete it there."
    ),
    # The post-install confirmation screen reads this and renders it as a
    # callout — Notion's per-page-share model is the most common source
    # of "I installed it but the bot can't see my pages" confusion.
    "post_install_callout": (
        "**Important:** the bot can only see Notion pages you've explicitly "
        "shared with this Integration. In Notion, open each page you want "
        "the bot to access, click ⋯ → Add connections → select your "
        "Integration. Pages aren't accessible until shared."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Notion install flow.

    State machine:

    * ``missing``   — no key stored yet.
    * ``valid``     — key present and Notion's /users/me returned ok.
    * ``revoked``   — key present but Notion returned 401 (deleted or expired).
    * ``invalid``   — key present but failed format checks.
    * ``unknown``   — pre-flight read failed; ``error`` has the detail.
    """

    bot_id: str
    status: str  # missing | valid | revoked | invalid | unknown
    workspace_name: str | None = None
    integration_name: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": NOTION_SKILL_ID,
            "status": self.status,
            "workspace_name": self.workspace_name,
            "integration_name": self.integration_name,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI drives. Same shape as the HA installer — credential
    form is rendered generically from ``fields[]``."""

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
    if status.status in ("valid", "unknown"):
        return []

    return [
        InstallStep(
            id="set_config",
            label="Paste your Notion integration key",
            endpoint=f"/api/skills/install/{NOTION_SKILL_ID}/set-token",
            payload={"bot_id": status.bot_id},
            fields=[
                {
                    "name": "access_token",
                    "label": "Internal integration secret",
                    "placeholder": "secret_… or ntn_…",
                    "type": "password",
                    "help": (
                        "Open notion.so → your profile → My integrations → "
                        "New integration (or pick an existing one) → copy the "
                        "Internal Integration Secret. Then share the pages "
                        "you want the bot to see with that integration."
                    ),
                },
            ],
            access_panel=dict(NOTION_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can read your Notion pages",
            endpoint=f"/api/skills/install/{NOTION_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── Token validation ──────────────────────────────────────────────────────────


def _token_looks_valid(token: str) -> bool:
    """Notion internal integration tokens are ``secret_`` (~50 chars) or
    ``ntn_`` (~50 chars). Be lenient on exact length — Notion has shifted
    formats before and may again."""
    if not token:
        return False
    t = token.strip()
    if len(t) < 30:
        return False
    return t.startswith("secret_") or t.startswith("ntn_")


def verify_token(token: str) -> dict:
    """Call ``GET /v1/users/me`` and check the bearer token works.

    Returns a dict with:
    * ``ok`` (bool)                  — True if Notion accepted the token.
    * ``status`` (str)               — valid | revoked | invalid | unknown.
    * ``workspace_name`` (str|None)
    * ``integration_name`` (str|None)
    * ``error`` (str|None)
    * ``http_status`` (int)
    """
    token = (token or "").strip()

    if not _token_looks_valid(token):
        return {
            "ok": False, "status": "invalid",
            "workspace_name": None, "integration_name": None,
            "error": "invalid_token_format", "http_status": 0,
        }

    status_code, body, err = _notion_get_json(
        f"{NOTION_API_BASE}/users/me", token,
    )

    if err == "connection_failed":
        return {
            "ok": False, "status": "unknown",
            "workspace_name": None, "integration_name": None,
            "error": "connection_failed", "http_status": 0,
        }

    if status_code == 401:
        return {
            "ok": False, "status": "revoked",
            "workspace_name": None, "integration_name": None,
            "error": "unauthorized", "http_status": 401,
        }

    if status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False, "status": "unknown",
            "workspace_name": None, "integration_name": None,
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    # Successful /users/me returns either a "person" or "bot" object;
    # internal integrations are bots. The bot.owner.workspace info tells
    # us which workspace this integration belongs to.
    bot_info = body.get("bot") or {}
    owner = bot_info.get("owner") or {}
    workspace_name = (
        owner.get("workspace_name")  # internal integrations
        or body.get("name")          # fallback for unexpected shapes
    )
    integration_name = body.get("name")

    return {
        "ok": True, "status": "valid",
        "workspace_name": workspace_name,
        "integration_name": integration_name,
        "error": None, "http_status": 200,
    }


def _notion_get_json(url: str, token: str) -> tuple[int, dict | None, str | None]:
    """GET a Notion API URL with bearer auth + version header."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Notion-Version", NOTION_API_VERSION)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=NOTION_HTTP_TIMEOUT_S) as resp:
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
        log.debug("notion_install: GET %s failed: %s", url, exc)
        return 0, None, "connection_failed"


# ── Config storage ────────────────────────────────────────────────────────────


def _config_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / NOTION_CONFIG_PATH


def read_config(bot_id: str) -> dict | None:
    p = _config_path(bot_id)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except PermissionError:
        pass
    except Exception:
        return None

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
    from ..config import bot_home as _bot_home, get_bot_user, load_network as _load_network

    network = _load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / NOTION_CONFIG_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-notion-", suffix=".json")
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
    check_token: Callable[[str], "dict"] | None = None,
) -> InstallStatus:
    _read = read_cfg or read_config
    _check = check_token or verify_token

    try:
        cfg = _read(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            error=f"config_read_failed: {exc.__class__.__name__}: {exc}",
        )

    if not cfg or not cfg.get("access_token"):
        return InstallStatus(bot_id=bot_id, status="missing")

    token = cfg["access_token"]
    if not _token_looks_valid(token):
        return InstallStatus(
            bot_id=bot_id, status="invalid",
            error="stored_token_format_invalid",
        )

    try:
        result = _check(token)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, status="unknown",
            workspace_name=cfg.get("workspace_name"),
            integration_name=cfg.get("integration_name"),
            error=f"verify_failed: {exc.__class__.__name__}: {exc}",
        )

    if result.get("ok"):
        return InstallStatus(
            bot_id=bot_id, status="valid",
            workspace_name=result.get("workspace_name") or cfg.get("workspace_name"),
            integration_name=result.get("integration_name") or cfg.get("integration_name"),
        )

    return InstallStatus(
        bot_id=bot_id,
        status=result.get("status") or "unknown",
        workspace_name=cfg.get("workspace_name"),
        integration_name=cfg.get("integration_name"),
        error=result.get("error"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": NOTION_SKILL_ID,
    "display_name": NOTION_ACCESS_PANEL["skill_display_name"],
    "summary": NOTION_ACCESS_PANEL["summary"],
    "access_panel": dict(NOTION_ACCESS_PANEL),
}


# ── MCP-install helpers (2026-05-30 rewire) ───────────────────────────────────
#
# Below this point is the new MCP-backed install path. The pre-2026-05-30
# helpers above (verify_token, read_config, write_config, delete_config,
# resolve_status) are kept because verify_token is still useful for the
# new flow (validates the secret before constructing the headers JSON), and
# the others are kept for backward-compat with any cached marker files
# on bots that ran the old paste-token install. The new flow does NOT
# write `~/.openclaw/skills/notion.json` — credentials live in the pod
# keystore instead, and the install state is read from
# `openclaw.json::mcp.servers.notion`.

NOTION_KEYSTORE_SLOT_PREFIX = "notion-"  # full slot = notion-<bot_id>
NOTION_MCP_SERVER_ID = "notion"  # key in openclaw.json::mcp.servers


def keystore_slot_for(bot_id: str) -> str:
    """Return the keystore slot name for this bot's Notion credentials.

    Per-bot scoping (mirrors Telegram's bot-token-per-bot pattern) — each
    bot can connect to a different Notion workspace by picking a
    different Internal Integration. Avoids the "every bot sees the same
    workspace" gotcha of pod-wide credentials.
    """
    return f"{NOTION_KEYSTORE_SLOT_PREFIX}{bot_id}"


def build_headers_json(token: str) -> str:
    """Construct the OPENAPI_MCP_HEADERS value for the Notion MCP server.

    ``@notionhq/notion-mcp-server`` reads a JSON-encoded headers blob
    from ``OPENAPI_MCP_HEADERS`` rather than discrete env vars. The
    documented shape (see source_url in catalog.py) is::

        {"Authorization": "Bearer <secret>", "Notion-Version": "2022-06-28"}

    This helper builds that string from a plain Internal Integration
    Secret paste. The wrapper route saves the result to the keystore so
    the launcher subsystem can resolve it at MCP-server-exec time. The
    secret never appears in openclaw.json (env_bindings only carries a
    ``keystore:<slot>`` reference).
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("token is required")
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
    }
    return json.dumps(headers, separators=(",", ":"))


# ── MCP-aware status resolver ────────────────────────────────────────────────


def resolve_status_mcp(
    bot_id: str,
    *,
    read_oc_config: Callable,  # (bot_id) -> (dict | None, err)
    read_keystore_slot: Callable | None = None,  # (slot) -> str | None
) -> InstallStatus:
    """Resolve the MCP-backed Notion install status.

    Mirrors Obsidian/Dropbox's resolve_status_mcp shape. Active when
    ``mcp.servers.notion`` is present in the bot's openclaw.json — that's
    the loader-side signal (§2 in inventory.py). The optional
    ``read_keystore_slot`` callable lets the resolver confirm the
    keystore reference still has a value; if the slot was wiped manually
    but the mcp.servers entry remains, we report ``revoked``.

    Returns:
      * ``valid`` — mcp.servers.notion present + keystore slot has a value
      * ``revoked`` — mcp.servers.notion present but keystore slot empty
      * ``missing`` — no mcp.servers.notion entry
      * ``unknown`` — openclaw.json could not be read
    """
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
            error=err or "no_openclaw_json",
        )

    servers = ((oc.get("mcp") or {}).get("servers") or {})
    entry = servers.get(NOTION_MCP_SERVER_ID)
    if not isinstance(entry, dict):
        return InstallStatus(bot_id=bot_id, status="missing")

    # Keystore-presence check (optional). When read_keystore_slot is
    # injected, an empty value flips status to ``revoked`` — covers the
    # case where an operator deleted the keystore slot via CLI without
    # uninstalling the MCP server entry.
    if read_keystore_slot is not None:
        try:
            value = read_keystore_slot(keystore_slot_for(bot_id))
        except Exception:
            value = None
        if not value:
            return InstallStatus(
                bot_id=bot_id, status="revoked",
                error="keystore_slot_empty",
            )

    return InstallStatus(bot_id=bot_id, status="valid")
