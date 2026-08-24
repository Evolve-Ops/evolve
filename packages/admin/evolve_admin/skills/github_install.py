"""evolve_admin.skills.github_install — GitHub-MCP install helpers (purpose 2).

GitHub serves three distinct purposes per the
``project_github_credentials_three_purposes`` memory:

  1. **Self-backup** — PAT in ``~/.openclaw/workspace/.git/config`` so the
     bot can push its workspace nightly. Handled by the Backup-page
     onboarding wizard (purpose-1 install lives in upstream_plugin_skills
     and short-circuits to ``open_github_backup_wizard``).
  2. **LLM-driven access** — the bot uses GitHub as a tool surface
     (search repos, read files, list/create issues, manage PRs). Routed
     through an MCP server. **This module owns purpose 2.**
  3. **Pod-wide developer issue tracking** — out of scope here; lives in
     a separate workflow not exposed as a per-bot skill.

The MCP install follows the Notion pattern established in #1831 — API-key
skill, credential lives in the pod keystore, wrapper route hides the
keystore plumbing from operators. The differences from Notion:

  * **Simpler env var.** GitHub MCP wants ``GITHUB_TOKEN`` (a plain
    Bearer-style PAT) rather than a JSON-encoded headers blob — no
    ``build_headers_json``-style transformation needed. The user-pasted
    token goes into the keystore verbatim.
  * **/user verification.** GitHub's ``GET /user`` (with the PAT as the
    Authorization header) is the cheapest way to confirm the token
    works. Returns ``login`` (username) and scopes via the
    ``X-OAuth-Scopes`` response header.
  * **No mode toggle.** Like Notion, scope is governed by the PAT's
    granted scopes (which the user chose when minting at
    github.com/settings/tokens). The wrapper just records what scopes
    the token has so the access panel can warn about over-scoped tokens.

Catalog entry the MCP install binds to lives at
``packages/analyzer/mcp_admin/catalog.py``::id="github" — the deprecated
``@modelcontextprotocol/server-github`` package. Anthropic stopped
maintaining this in favor of GitHub-owned alternatives; see the PR
description for the follow-up question on swapping packages. The
catalog entry's ``vetting_status="approved"`` reflects the original
2026-05-10 source review and hasn't been re-verified since.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

#: Skill id for the MCP-install purpose. NB: the existing
#: upstream_plugin_skills github skill ALSO uses id="github" — that's the
#: purpose-1 (backup) install. The mcp.servers entry below uses the same
#: server_id "github". Catalog id is also "github". Distinct concerns,
#: shared string.
GITHUB_SKILL_ID = "github"

#: Server id in the bot's openclaw.json::mcp.servers block. Matches the
#: catalog id since the catalog entry is the only github MCP install
#: source today; a future bot with N github MCP installs would need
#: distinct server_ids per install.
GITHUB_MCP_SERVER_ID = "github"

#: GitHub API base URL.
GITHUB_API_BASE = "https://api.github.com"

#: HTTP timeout for /user verification calls.
GITHUB_HTTP_TIMEOUT_S = 10


# ── Keystore slot naming ──────────────────────────────────────────────────────

#: Pod-wide keystore slot for the default General-code-access PAT. Any bot
#: with the GitHub MCP server enabled reads this slot unless it has its own
#: per-bot override (see :func:`keystore_slot_for`). The slot name parallels
#: ``github_intake`` — both are pod-wide, both rotated through the existing
#: intake-token modal, both stored in the same keystore backend.
POD_KEYSTORE_SLOT = "github_general_access"


def keystore_slot_for(bot_id: str) -> str:
    """Return the per-bot override slot for this bot's GitHub MCP token.

    Resolution model: a bot reads its own slot first, falling back to
    :data:`POD_KEYSTORE_SLOT` if empty. This lets the operator manage one
    pod-wide PAT by default while still allowing a per-bot override for
    bots that need their own scopes / repo allowlist (e.g. a sandboxed
    bot that should only see one repo, or a bot whose calls should be
    auditable as a distinct GitHub identity).

    Note this is DIFFERENT from the PAT used for purpose 1 (backup),
    which lives in ``.git/config`` directly and is unaffected by either
    slot above. A bot can have either, both, or neither.
    """
    return f"github-{bot_id}"


def resolve_github_token(
    bot_id: str,
    *,
    read_keystore_slot: Callable[[str], str | None],
) -> tuple[str | None, str]:
    """Resolve the effective General-code-access token for ``bot_id``.

    Returns ``(token, source)`` where ``source`` is one of:

    * ``"override"`` — per-bot slot held a value; pod-wide ignored.
    * ``"pod"`` — per-bot slot empty; pod-wide slot held a value.
    * ``"none"`` — both slots empty.

    The caller supplies the keystore reader so this stays pure for tests.
    Exceptions from the reader fold into "not found" rather than raising —
    a missing keystore is operationally equivalent to an empty slot for
    the resolution decision.
    """
    def _read(slot: str) -> str | None:
        try:
            v = read_keystore_slot(slot)
        except Exception:  # noqa: BLE001
            return None
        return v if (isinstance(v, str) and v.strip()) else None

    override = _read(keystore_slot_for(bot_id))
    if override:
        return override, "override"
    pod = _read(POD_KEYSTORE_SLOT)
    if pod:
        return pod, "pod"
    return None, "none"


# ── Token verification ────────────────────────────────────────────────────────


def verify_token(token: str) -> dict:
    """Verify a PAT against ``GET /user``. Returns a dict with:

    * ``ok`` (bool)
    * ``status`` (str) — valid | revoked | invalid | unknown
    * ``username`` (str | None) — GitHub login on success
    * ``scopes`` (list[str]) — comma-separated X-OAuth-Scopes split
    * ``error`` (str | None)
    * ``http_status`` (int)

    Strictness is lighter than Notion's because GitHub PATs come in
    multiple formats (classic ``ghp_*``, fine-grained ``github_pat_*``,
    and even no-prefix legacy) — we let the API reject bad formats
    rather than format-check up front.
    """
    token = (token or "").strip()
    if not token:
        return {
            "ok": False, "status": "invalid",
            "username": None, "scopes": [],
            "error": "empty_token", "http_status": 0,
        }

    status_code, body, scopes_header, err = _github_get_user(token)

    if err == "connection_failed":
        return {
            "ok": False, "status": "unknown",
            "username": None, "scopes": [],
            "error": "connection_failed", "http_status": 0,
        }

    if status_code == 401:
        return {
            "ok": False, "status": "revoked",
            "username": None, "scopes": [],
            "error": "unauthorized", "http_status": 401,
        }

    if status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False, "status": "unknown",
            "username": None, "scopes": [],
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    scopes = [s.strip() for s in (scopes_header or "").split(",") if s.strip()]
    return {
        "ok": True, "status": "valid",
        "username": body.get("login"),
        "scopes": scopes,
        "error": None, "http_status": 200,
    }


def _github_get_user(token: str) -> tuple[int, dict | None, str | None, str | None]:
    """GET https://api.github.com/user with bearer auth.

    Returns ``(http_status, body, x_oauth_scopes_header, err)``. The
    scopes header is split-and-parsed by the caller — we just hand it
    back raw to keep this helper simple. ``err='connection_failed'``
    is the connection-level error sentinel (vs HTTP-level 4xx/5xx).
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(f"{GITHUB_API_BASE}/user")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            scopes_hdr = resp.headers.get("X-OAuth-Scopes")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = None
            return resp.status, body, scopes_hdr, None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else None
        except Exception:
            body = None
        scopes_hdr = e.headers.get("X-OAuth-Scopes") if e.headers else None
        return e.code, body, scopes_hdr, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None, None, "connection_failed"


# ── Plain-language access panel ───────────────────────────────────────────────

GITHUB_MCP_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GITHUB_SKILL_ID,
    "skill_display_name": "GitHub (LLM access via MCP)",
    "summary": (
        "Lets this bot search, read, and act on GitHub repositories you "
        "give it access to. Useful for code-aware briefings, issue "
        "triage, PR drafts, and pulling file contents into conversation. "
        "Distinct from GitHub backup (which uses the same site but for "
        "pushing this bot's own workspace to a private repo — managed on "
        "the Backup page)."
    ),
    "will": [
        "Search repositories your token can see",
        "Read file contents from those repositories",
        "List commits, pull requests, and issues",
        "Open issues and pull requests when you ask it to",
    ],
    "wont": [
        "Access any repository your token doesn't grant (GitHub enforces this)",
        "Make destructive changes (force-push, repo delete) without your ask",
        "Share your token outside this bot",
        "Touch repositories outside the scopes you minted the token with",
    ],
    "where_credentials_live": (
        "Your personal access token is stored only in this pod's "
        "keystore — never centralised, never sent off-pod. To revoke "
        "access, delete the token at github.com/settings/tokens (or "
        "uninstall this skill via the admin UI, which blanks the "
        "keystore slot)."
    ),
    # Surface scope information to the operator AFTER the token is
    # verified — minting a token with `repo` when you only need `repo:read`
    # is the #1 over-scope mistake. The wrapper route adds the verified
    # scopes to the response payload.
    "post_install_callout": (
        "**Tip:** mint your token at github.com/settings/tokens with the "
        "narrowest scope your bot needs. `repo:read` (read-only) is "
        "enough for browsing and reading; only add `repo` (read+write) "
        "if the bot needs to push commits or open PRs."
    ),
}


# ── Install status ────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of the github MCP install state.

    * ``valid``    — mcp.servers.github present + keystore slot has a value
    * ``revoked``  — mcp.servers.github present but keystore slot empty
    * ``missing``  — no mcp.servers.github entry
    * ``unknown``  — openclaw.json could not be read
    """

    bot_id: str
    status: str
    username: str | None = None
    scopes: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": GITHUB_SKILL_ID,
            "status": self.status,
            "username": self.username,
            "scopes": list(self.scopes),
            "error": self.error,
        }


# ── MCP-aware status resolver ────────────────────────────────────────────────


def resolve_status_mcp(
    bot_id: str,
    *,
    read_oc_config: Callable,  # (bot_id) -> (dict | None, err)
    read_keystore_slot: Callable | None = None,  # (slot) -> str | None
) -> InstallStatus:
    """Resolve the MCP-backed github install status.

    Mirrors Notion's resolve_status_mcp exactly — same shape, same
    optional keystore-presence probe. The github SKILL might also
    report as "configured" via inventory.py §5 (workspace .git/config
    detection — purpose 1, backup), but that's a different surface and
    not consulted here. This resolver answers ONLY the "is the MCP
    server installed?" question.
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
    entry = servers.get(GITHUB_MCP_SERVER_ID)
    if not isinstance(entry, dict):
        return InstallStatus(bot_id=bot_id, status="missing")

    if read_keystore_slot is not None:
        token, source = resolve_github_token(
            bot_id, read_keystore_slot=read_keystore_slot,
        )
        if not token:
            return InstallStatus(
                bot_id=bot_id, status="revoked",
                error="keystore_slot_empty",
            )
        # Pod-wide fallback is the success path; "override" just means the
        # bot is using its own slot. Both render as valid for the install
        # state — the API surfaces the source separately for the UI.

    return InstallStatus(bot_id=bot_id, status="valid")


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": GITHUB_SKILL_ID,
    "display_name": GITHUB_MCP_ACCESS_PANEL["skill_display_name"],
    "summary": GITHUB_MCP_ACCESS_PANEL["summary"],
    "access_panel": dict(GITHUB_MCP_ACCESS_PANEL),
}
