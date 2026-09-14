"""evolve_admin.skills.linear_install — Linear skill install flow.

.. note::
   **Rewired 2026-05-30 as the fifth MCP-backed install.** Linear uses
   the same shape as Notion / GitHub-MCP: API-key skill, credentials in
   the pod keystore, validate-against-API before keystore write.
   Difference from Notion: the MCP server is the community ``linear-mcp``
   package (by dvcrn, MIT) rather than first-party — see catalog.py
   vetting_notes for the trust trade-off and version-pin recommendation.
   Difference from GitHub-MCP: Linear's permission model is
   workspace-membership, not API-scope — the token inherits whatever the
   user can see; the access panel surfaces this in the post_install_callout.

   OC audit (2026-05-30): Linear is NOT bundled in OC's
   ``dist/extensions/`` directory, so the MCP path is the right choice
   here (vs the bundled-plugin pattern used for Runway).

Linear is the issue tracker that Carla's engineering-flavoured clients and
Marcus's matter pipeline use. Adding it as an installable skill lets a bot
list issues, draft comments, post on the user's behalf, and pull "what's
on my plate" context — the same shape as the Notion installer.

Auth shape (personal API key, not OAuth)
----------------------------------------
Linear supports both OAuth and personal API keys. For the Plex-test
audience, personal API key is the right default — no app registration,
no redirect URIs, no developer flow. The user generates one at:

  linear.app → Settings → API → Personal API keys → New API key

…and pastes it. Tokens have the form ``lin_api_<characters>``.

Notable wire-protocol detail: Linear's REST/GraphQL endpoint expects the
Authorization header WITHOUT the ``Bearer`` prefix when authenticating
with a personal API key (the prefix is used for OAuth tokens only). This
is the load-bearing wrinkle vs Notion / Home Assistant.

Token storage
-------------
Token + workspace metadata stored at
``<bot_home>/.openclaw/skills/linear.json`` (bot-user-owned, /tmp staging
+ sudo /bin/cp per CLAUDE.md). Revocation: user deletes the API key at
linear.app — there is no remote-revoke API for the key holder, so revoke
is local-only on our side.
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

LINEAR_SKILL_ID = "linear"
LINEAR_CONFIG_PATH = ".openclaw/skills/linear.json"
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

LINEAR_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": LINEAR_SKILL_ID,
    "skill_display_name": "Linear",
    "summary": (
        "Lets this bot read and act on your Linear issues — list what's on "
        "your plate, draft comments, surface team status, and create or "
        "update issues when you ask it to."
    ),
    "will": [
        "List issues, projects, and teams in your Linear workspace",
        "Read issue contents, comments, and status",
        "Create issues, update status, and post comments when you ask",
    ],
    "wont": [
        "Delete issues or projects without your explicit confirmation",
        "Change settings, members, or billing in your workspace",
        "Share your Linear data with anyone outside this bot",
    ],
    "where_credentials_live": (
        "Your Linear personal API key is stored only in this pod's keystore — "
        "never centralised, never sent off-pod. To revoke, delete the key at "
        "linear.app → Settings → API → Personal API keys (Linear doesn't "
        "expose a token-holder-side revoke API)."
    ),
    # Linear's permission model is workspace-membership-based, not
    # API-scope-based. The token inherits whatever the user can see + do.
    # Most common surprise is "the bot acts as me" — flag upfront.
    "post_install_callout": (
        "**Heads-up on identity:** the bot acts as the Linear user who "
        "minted the API key. Issues created and comments posted will show "
        "that user as the author. If you want a separate identity, mint "
        "the key from a dedicated bot user account (linear.app → Settings "
        "→ Members → invite a new user)."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Linear install flow.

    State machine:

    * ``missing``   — no key stored yet.
    * ``valid``     — key present and Linear's /graphql viewer query returned ok.
    * ``revoked``   — key present but Linear returned 401/unauthorized (deleted
                      or expired).
    * ``invalid``   — key present but failed format checks (no lin_api_ prefix,
                      too short, etc.).
    * ``unknown``   — pre-flight read failed; ``error`` has the detail.
    """

    bot_id: str
    status: str
    workspace_name: str | None = None
    viewer_name: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": LINEAR_SKILL_ID,
            "status": self.status,
            "workspace_name": self.workspace_name,
            "viewer_name": self.viewer_name,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


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
    if status.status in ("valid", "unknown"):
        return []

    return [
        InstallStep(
            id="set_config",
            label="Paste your Linear personal API key",
            endpoint=f"/api/skills/install/{LINEAR_SKILL_ID}/set-token",
            payload={"bot_id": status.bot_id},
            fields=[
                {
                    "name": "access_token",
                    "label": "Personal API key",
                    "placeholder": "lin_api_…",
                    "type": "password",
                    "help": (
                        "Open linear.app → Settings (lower left) → API → "
                        "Personal API keys → New API key. Name it something "
                        "like 'Evolve bot', then copy and paste the value here."
                    ),
                },
            ],
            access_panel=dict(LINEAR_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can read your Linear workspace",
            endpoint=f"/api/skills/install/{LINEAR_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── Token format validation ───────────────────────────────────────────────────


def _token_looks_valid(token: str) -> bool:
    """Linear personal API keys are 'lin_api_<characters>', ~50 chars total.
    Older keys (pre-2023) sometimes lacked the prefix; we accept both shapes
    above a sensible minimum length so we don't reject the user's legitimate
    legacy key — Linear's API will be the authoritative judge."""
    if not token:
        return False
    t = token.strip()
    if len(t) < 30:
        return False
    return True


# ── Token verification via GraphQL ────────────────────────────────────────────


def verify_token(token: str) -> dict:
    """POST a tiny ``{viewer{id name}, organization{name}}`` query to Linear.

    Returns a dict with:
    * ``ok`` (bool)
    * ``status`` (str)         — valid | revoked | invalid | unknown
    * ``workspace_name`` (str|None)
    * ``viewer_name`` (str|None)
    * ``error`` (str|None)
    * ``http_status`` (int)
    """
    token = (token or "").strip()
    if not _token_looks_valid(token):
        return {
            "ok": False, "status": "invalid",
            "workspace_name": None, "viewer_name": None,
            "error": "invalid_token_format", "http_status": 0,
        }

    query = "{ viewer { id name email } organization { id name } }"
    status_code, body, err = _linear_post_graphql(token, query)

    if err == "connection_failed":
        return {
            "ok": False, "status": "unknown",
            "workspace_name": None, "viewer_name": None,
            "error": "connection_failed", "http_status": 0,
        }

    # Linear returns 200 + errors in body on bad auth via personal API keys.
    # Treat any 401/403 or a body with auth-shaped errors as revoked.
    if status_code in (401, 403):
        return {
            "ok": False, "status": "revoked",
            "workspace_name": None, "viewer_name": None,
            "error": "unauthorized", "http_status": status_code,
        }

    if status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False, "status": "unknown",
            "workspace_name": None, "viewer_name": None,
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    if body.get("errors"):
        # GraphQL-level errors. Inspect the first one for auth-shaped messages
        # — Linear surfaces these as 200 + {errors:[{extensions.code:...}]}.
        first = body["errors"][0]
        code = ((first.get("extensions") or {}).get("code") or "")
        msg = first.get("message") or ""
        is_auth_err = (
            code in ("AUTHENTICATION_ERROR", "UNAUTHENTICATED")
            or "Authentication" in msg
            or "Unauthorized" in msg
        )
        return {
            "ok": False,
            "status": "revoked" if is_auth_err else "unknown",
            "workspace_name": None, "viewer_name": None,
            "error": msg or "graphql_error",
            "http_status": status_code,
        }

    data = body.get("data") or {}
    org = data.get("organization") or {}
    viewer = data.get("viewer") or {}
    return {
        "ok": True, "status": "valid",
        "workspace_name": org.get("name"),
        "viewer_name": viewer.get("name") or viewer.get("email"),
        "error": None, "http_status": 200,
    }


def _linear_post_graphql(token: str, query: str) -> tuple[int, dict | None, str | None]:
    """POST a GraphQL query to Linear with personal API key auth.

    Linear's personal API keys go in the Authorization header WITHOUT
    the 'Bearer' prefix — that prefix is only for OAuth access tokens.
    """
    import urllib.request
    import urllib.error

    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(LINEAR_API_URL, data=body, method="POST")
    req.add_header("Authorization", token)  # no Bearer for personal API keys
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=LINEAR_HTTP_TIMEOUT_S) as resp:
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
        log.debug("linear_install: POST failed: %s", exc)
        return 0, None, "connection_failed"


# ── Config storage ────────────────────────────────────────────────────────────


def _config_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / LINEAR_CONFIG_PATH


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
    dest = str(home / LINEAR_CONFIG_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-linear-", suffix=".json")
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
            viewer_name=cfg.get("viewer_name"),
            error=f"verify_failed: {exc.__class__.__name__}: {exc}",
        )

    if result.get("ok"):
        return InstallStatus(
            bot_id=bot_id, status="valid",
            workspace_name=result.get("workspace_name") or cfg.get("workspace_name"),
            viewer_name=result.get("viewer_name") or cfg.get("viewer_name"),
        )

    return InstallStatus(
        bot_id=bot_id,
        status=result.get("status") or "unknown",
        workspace_name=cfg.get("workspace_name"),
        viewer_name=cfg.get("viewer_name"),
        error=result.get("error"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": LINEAR_SKILL_ID,
    "display_name": LINEAR_ACCESS_PANEL["skill_display_name"],
    "summary": LINEAR_ACCESS_PANEL["summary"],
    "access_panel": dict(LINEAR_ACCESS_PANEL),
}


# ── MCP-install helpers (2026-05-30 rewire) ───────────────────────────────────
#
# Below this point is the new MCP-backed install path. The pre-2026-05-30
# helpers above (verify_token, read_config, write_config, delete_config,
# resolve_status) are kept because verify_token is still useful for the
# new flow (validates the secret before keystore write), and the others
# are kept for backward-compat with any cached marker files on bots that
# ran the old paste-token install. The new flow does NOT write
# `~/.openclaw/skills/linear.json` — credentials live in the pod keystore
# instead, and the install state is read from `openclaw.json::mcp.servers.linear`.
#
# Difference from Notion's MCP install: Linear's MCP server takes the API
# key as a plain ``LINEAR_API_KEY`` env var (the @linear/sdk shape) — no
# JSON-encoded headers blob. The keystore stores the verbatim secret;
# env_bindings references the slot directly. This mirrors GitHub-MCP's
# verbatim-PAT shape.

LINEAR_KEYSTORE_SLOT_PREFIX = "linear-"  # full slot = linear-<bot_id>
LINEAR_MCP_SERVER_ID = "linear"  # key in openclaw.json::mcp.servers


def keystore_slot_for(bot_id: str) -> str:
    """Return the keystore slot name for this bot's Linear credentials.

    Per-bot scoping (mirrors Telegram / Notion / GitHub-MCP bot-token-per-bot
    pattern) — each bot can connect to a different Linear workspace by
    minting the API key under a different Linear user account. Avoids the
    "every bot sees the same workspace" gotcha of pod-wide credentials.
    """
    return f"{LINEAR_KEYSTORE_SLOT_PREFIX}{bot_id}"


# ── MCP-aware status resolver ────────────────────────────────────────────────


def resolve_status_mcp(
    bot_id: str,
    *,
    read_oc_config: Callable,  # (bot_id) -> (dict | None, err)
    read_keystore_slot: Callable | None = None,  # (slot) -> str | None
) -> InstallStatus:
    """Resolve the MCP-backed Linear install status.

    Mirrors Notion/GitHub-MCP's resolve_status_mcp shape. Active when
    ``mcp.servers.linear`` is present in the bot's openclaw.json — that's
    the loader-side signal (§2 in inventory.py). The optional
    ``read_keystore_slot`` callable lets the resolver confirm the
    keystore reference still has a value; if the slot was wiped manually
    but the mcp.servers entry remains, we report ``revoked``.

    Returns:
      * ``valid`` — mcp.servers.linear present + keystore slot has a value
      * ``revoked`` — mcp.servers.linear present but keystore slot empty
      * ``missing`` — no mcp.servers.linear entry
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
    entry = servers.get(LINEAR_MCP_SERVER_ID)
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
