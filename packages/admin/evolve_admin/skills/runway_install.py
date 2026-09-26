"""evolve_admin.skills.runway_install — Runway skill install flow.

.. note::
   **Rewired 2026-05-30 as the FIRST bundled-plugin install** — a new
   pattern alongside the MCP install pattern Obsidian (#1817) / Dropbox
   (#1819) / Notion (#1831) / GitHub-MCP (#1832) established. Runway
   doesn't need an MCP server because OpenClaw ships
   ``@openclaw/runway-provider`` bundled internally as a
   ``videoGenerationProviders`` contract provider. The install is purely
   OC-config: write an auth-profiles.json entry + an openclaw.json
   model-default + kickstart the gateway. The bundled plugin auto-loads
   and exposes the ``video_generate`` tool to the bot.

Two install patterns now coexist (see
``internal/design/skills-install-roadmap-2026-05-30.md``):

  * **MCP install** (Obsidian, Dropbox, Notion, GitHub-MCP) — keystore
    + env_bindings + InstallMcpServer proposal → ``mcp.servers.<id>``
    in openclaw.json. Used for external-MCP-server skills.
  * **Bundled-plugin install** (Runway, GOG, future Veo etc.) —
    auth-profiles.json profile + openclaw.json plugin config +
    gateway kickstart. Used for skills OpenClaw already supports
    via shipped first-party plugins.

Runway is a creative AI platform — video generation, image generation,
upscaling, in/outpainting. The bundled ``@openclaw/runway-provider``
exposes:

  * ``video_generate(model, ...)`` — task-based, OC handles polling
  * Models: ``runway/gen4.5`` (default, text+image → video),
    ``runway/gen4_turbo`` / ``runway/gen3a_turbo`` (image → video),
    ``runway/gen4_aleph`` (video → video — required for v2v mode)
  * Aspect ratios: text2video supports 16:9 + 9:16; image/video edits
    add 1:1, 3:4, 4:3, 21:9
  * Pinned API version via X-Runway-Version (the provider handles this)

Auth shape (API key paste, not OAuth)
--------------------------------------
Runway issues per-user / per-organisation API keys via app.runwayml.com
→ API Keys. Format is ``key_`` followed by 128 hex characters. There is
no OAuth flow for the developer API — it's a paste-token shape, same
ergonomic class as Notion's internal-integration secret.

Where the key lands
-------------------
``~/.openclaw/agents/main/agent/auth-profiles.json`` under
``profiles["runway:default"]``::

    {
      "type": "api_key",
      "provider": "runway",
      "key": "<the secret>"
    }

OpenClaw reads this at startup. Same shape as ``google:default`` /
``xai:default`` / etc. — we use the existing :func:`_write_auth_profiles`
plumbing from server.py (same sudo+chown choreography as Google OAuth).

Plus a one-line update to openclaw.json::

    {"agents": {"defaults": {"videoGenerationModel": {"primary": "runway/gen4.5"}}}}

Revocation
----------
Local revoke deletes ``profiles["runway:default"]`` and unsets the
videoGenerationModel default. To fully revoke, delete the key in the
Runway dashboard — Runway doesn't expose a token-holder-side revoke API.
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

log = logging.getLogger(__name__)


# ── Skill identifier ──────────────────────────────────────────────────────────

RUNWAY_SKILL_ID = "runway"
RUNWAY_CONFIG_PATH = ".openclaw/skills/runway.json"

#: API base. ``api.dev.runwayml.com`` is the documented developer API host
#: (per the error response we get on a 401, which links to docs.dev.runwayml.com).
RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"

#: X-Runway-Version pin. Bump in code when Runway ships a new version we
#: want to opt into; pinning protects us from silent behaviour drift.
RUNWAY_API_VERSION = "2024-11-06"

#: HTTP timeout.
RUNWAY_HTTP_TIMEOUT_S = 10


# ── Plain-language access panel ───────────────────────────────────────────────

RUNWAY_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": RUNWAY_SKILL_ID,
    "skill_display_name": "Runway",
    "summary": (
        "Lets this bot generate video and images via Runway — turn a "
        "brief into a short clip, generate or edit images, upscale "
        "assets. Useful when you want the bot to produce visual content "
        "as part of a workflow. Uses OpenClaw's bundled Runway provider; "
        "no extra plugin or MCP server install required."
    ),
    "will": [
        "Submit text-to-video and image-to-video generation jobs",
        "Generate, upscale, and edit images with Runway's models",
        "Track the status of jobs you have queued (polled automatically)",
    ],
    "wont": [
        "Spend on jobs you didn't ask for",
        "Share generated content with anyone outside this bot",
        "Access projects belonging to other Runway accounts",
    ],
    "where_credentials_live": (
        "Your Runway API key is stored only on this bot's user account "
        "in ~/.openclaw/agents/main/agent/auth-profiles.json (same file "
        "and shape as your Google / xAI / Anthropic provider keys). "
        "Never centralised, never sent off-pod. To fully revoke, also "
        "delete the key in the Runway dashboard → API Keys (Runway "
        "doesn't expose a token-holder-side revoke API)."
    ),
    # The post-install confirmation screen reads this and renders it as
    # a callout — Runway charges per-second of generated video, so a
    # cost-awareness nudge is part of the safe-default UX.
    "post_install_callout": (
        "**Heads-up on cost:** Runway charges per-second of generated "
        "video (~$0.05/s for gen4.5 at v1 pricing). Bots will use "
        "Runway when you ask them to; they don't generate spontaneously. "
        "Check your Runway dashboard's usage page if you want a hard cap."
    ),
}


# ── Install status ─────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Runway install flow.

    States:

    * ``missing``  — no key stored yet.
    * ``valid``    — key present and Runway's /organization returned ok.
    * ``revoked``  — key present but Runway returned 401 (deleted/expired).
    * ``invalid``  — key fails format check (wrong prefix, wrong length).
    * ``unknown``  — pre-flight read failed or Runway unreachable.
    """

    bot_id: str
    status: str
    organization_name: str | None = None
    organization_tier: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": RUNWAY_SKILL_ID,
            "status": self.status,
            "organization_name": self.organization_name,
            "organization_tier": self.organization_tier,
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
            label="Paste your Runway API key",
            endpoint=f"/api/skills/install/{RUNWAY_SKILL_ID}/set-token",
            payload={"bot_id": status.bot_id},
            fields=[
                {
                    "name": "access_token",
                    "label": "API key",
                    "placeholder": "key_…",
                    "type": "password",
                    "help": (
                        "Open app.runwayml.com → your profile → API Keys → "
                        "New API key. Name it something like 'Evolve bot', "
                        "then copy and paste the value here. Keys start "
                        "with key_ followed by 128 hex characters."
                    ),
                },
            ],
            access_panel=dict(RUNWAY_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can talk to Runway",
            endpoint=f"/api/skills/install/{RUNWAY_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── Token format validation ───────────────────────────────────────────────────

#: Runway keys: ``key_`` + 128 hex chars (per Runway's own error message
#: when an invalid-length key is sent). Be strict about format so we don't
#: round-trip a network call for an obviously wrong paste.
_TOKEN_PATTERN = re.compile(r"^key_[0-9a-fA-F]{128}$")


def _token_looks_valid(token: str) -> bool:
    if not token:
        return False
    return bool(_TOKEN_PATTERN.match(token.strip()))


# ── Token verification ────────────────────────────────────────────────────────


def verify_token(token: str) -> dict:
    """Call ``GET /v1/organization`` and check the bearer token works.

    Returns a dict with:
    * ``ok`` (bool)
    * ``status`` (str)               — valid | revoked | invalid | unknown
    * ``organization_name`` (str|None)
    * ``organization_tier`` (str|None)
    * ``error`` (str|None)
    * ``http_status`` (int)
    """
    token = (token or "").strip()
    if not _token_looks_valid(token):
        return {
            "ok": False, "status": "invalid",
            "organization_name": None, "organization_tier": None,
            "error": "invalid_token_format", "http_status": 0,
        }

    status_code, body, err = _runway_get_json(
        f"{RUNWAY_API_BASE}/organization", token,
    )

    if err == "connection_failed":
        return {
            "ok": False, "status": "unknown",
            "organization_name": None, "organization_tier": None,
            "error": "connection_failed", "http_status": 0,
        }

    if status_code == 401:
        return {
            "ok": False, "status": "revoked",
            "organization_name": None, "organization_tier": None,
            "error": "unauthorized", "http_status": 401,
        }

    if status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False, "status": "unknown",
            "organization_name": None, "organization_tier": None,
            "error": f"http_error_{status_code}",
            "http_status": status_code,
        }

    # /v1/organization shape: {id, name, tier (free/pro/enterprise), ...}.
    # Surfacing both name + tier lets the success message say "...in <Org>
    # (Pro tier)" so the user knows which workspace + plan they wired up.
    return {
        "ok": True, "status": "valid",
        "organization_name": body.get("name"),
        "organization_tier": body.get("tier"),
        "error": None, "http_status": 200,
    }


def _runway_get_json(url: str, token: str) -> tuple[int, dict | None, str | None]:
    """GET a Runway API URL with bearer auth + version header.

    Runway requires X-Runway-Version on every call; missing it produces a
    400 with an actionable error. We pin RUNWAY_API_VERSION so behaviour
    doesn't drift when Runway ships a new version.
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Runway-Version", RUNWAY_API_VERSION)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=RUNWAY_HTTP_TIMEOUT_S) as resp:
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
        log.debug("runway_install: GET %s failed: %s", url, exc)
        return 0, None, "connection_failed"


# ── Config storage ────────────────────────────────────────────────────────────


def _config_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / RUNWAY_CONFIG_PATH


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
    dest = str(home / RUNWAY_CONFIG_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-runway-", suffix=".json")
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
            organization_name=cfg.get("organization_name"),
            organization_tier=cfg.get("organization_tier"),
            error=f"verify_failed: {exc.__class__.__name__}: {exc}",
        )

    if result.get("ok"):
        return InstallStatus(
            bot_id=bot_id, status="valid",
            organization_name=result.get("organization_name") or cfg.get("organization_name"),
            organization_tier=result.get("organization_tier") or cfg.get("organization_tier"),
        )

    return InstallStatus(
        bot_id=bot_id,
        status=result.get("status") or "unknown",
        organization_name=cfg.get("organization_name"),
        organization_tier=cfg.get("organization_tier"),
        error=result.get("error"),
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": RUNWAY_SKILL_ID,
    "display_name": RUNWAY_ACCESS_PANEL["skill_display_name"],
    "summary": RUNWAY_ACCESS_PANEL["summary"],
    "access_panel": dict(RUNWAY_ACCESS_PANEL),
}


# ── Bundled-plugin install helpers (2026-05-30 rewire) ───────────────────────
#
# Below this point is the new install path. Pre-2026-05-30 helpers above
# (verify_token, read_config, write_config, delete_config, resolve_status)
# are kept because verify_token still validates the API key against
# Runway's /v1/organization before any auth-profiles.json write. The
# other helpers are kept for backward-compat with bots that ran the
# old paste-token install — the new flow does NOT write
# ``~/.openclaw/skills/runway.json``; the credential lives in
# ``auth-profiles.json`` under ``profiles["runway:default"]``.

#: Profile id in auth-profiles.json. Matches what OC's bundled
#: ``@openclaw/runway-provider`` reads at startup.
RUNWAY_AUTH_PROFILE_ID = "runway:default"

#: Default model the install enables. The reference deployment uses gen4.5
#: as the default; the model can be overridden per-request by the bot.
RUNWAY_DEFAULT_MODEL = "runway/gen4.5"


def auth_profiles_path(bot_id: str) -> Path:
    """Return the auth-profiles.json path for *bot_id*.

    Path matches what OC's bundled providers read from at startup.
    Same location used by Google OAuth, xAI, etc. — see
    server.py::_write_auth_profiles for the canonical write helper this
    mirrors.
    """
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"


def read_auth_profiles(bot_id: str) -> dict:
    """Read the bot's auth-profiles.json. Returns {} if missing/unreadable.

    Direct read first (ACL set by set_evolve_read_acl during deploy),
    sudo /bin/cat fallback.
    """
    p = auth_profiles_path(bot_id)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (PermissionError, OSError, json.JSONDecodeError):
        pass
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return {}


def write_auth_profiles(bot_id: str, data: dict) -> tuple[bool, str | None]:
    """Atomically write auth-profiles.json via /tmp staging + sudo /bin/cp.

    Mirrors server.py::_write_auth_profiles's 5-step choreography
    (mkdir + chown parent + cp + chown file + chmod 600). The parent
    chown is load-bearing: OC does atomic writes (.tmp + rename) for
    its own token-refresh cycles, which requires the parent dir to be
    bot-user-writable. Without that, OC's auth-profile updates fail
    EACCES (see #1816 for the bug-from-omitting-this).

    File perms are 600 because the file contains API keys. openclaw.json is
    likewise 600 (it holds the gateway + channel tokens); the evolve admin
    user reads both via the inherited .openclaw read ACL, not a world bit.
    """
    from ..config import bot_home as _bot_home, get_bot_user, load_network

    network = load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json")
    parent_dir = str(home / ".openclaw" / "agents" / "main" / "agent")

    content = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-rw-auth-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)

        # Step 1: mkdir -p the parent (may leave it root-owned).
        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", parent_dir],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"mkdir failed: {(r.stderr or '').strip() or 'unknown'}"

        # Step 2: chown parent → bot:staff so OC can atomically write tmp files.
        # Load-bearing per #1816 — without this, OC's auth-profile refresh
        # cycle EACCESes on the .tmp creation in the parent dir.
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", parent_dir],
            capture_output=True, text=True, timeout=10,
        )

        # Step 3: cp /tmp staged file to dest (will be root-owned).
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, dest],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"cp failed: {(r.stderr or '').strip() or 'unknown'}"

        # Step 4: chown dest → bot:staff so the bot can read its own creds.
        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", dest],
            capture_output=True, text=True, timeout=10,
        )

        # Step 5: chmod 600 — API keys → not 644.
        subprocess.run(
            ["sudo", "/bin/chmod", "600", dest],
            capture_output=True, text=True, timeout=10,
        )
        return True, None
    except Exception as exc:
        return False, f"write_auth_profiles_error: {exc.__class__.__name__}: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def write_runway_auth_profile(
    bot_id: str, api_key: str,
) -> tuple[bool, str | None]:
    """Write the runway:default profile into auth-profiles.json.

    Preserves any other profiles already in the file (google:default,
    xai:default, etc.). Only mutates ``profiles["runway:default"]``.
    Returns (ok, error).
    """
    data = read_auth_profiles(bot_id) or {}
    profiles = data.setdefault("profiles", {})
    profiles[RUNWAY_AUTH_PROFILE_ID] = {
        "type": "api_key",
        "provider": "runway",
        "key": (api_key or "").strip(),
    }
    return write_auth_profiles(bot_id, data)


def delete_runway_auth_profile(bot_id: str) -> tuple[bool, str | None]:
    """Remove the runway:default profile from auth-profiles.json.

    Idempotent — absent profile + missing file both return True. Other
    profiles are preserved.
    """
    data = read_auth_profiles(bot_id)
    if not data or not data.get("profiles", {}).get(RUNWAY_AUTH_PROFILE_ID):
        return True, None
    del data["profiles"][RUNWAY_AUTH_PROFILE_ID]
    return write_auth_profiles(bot_id, data)


# ── openclaw.json model-default wiring ────────────────────────────────────────


def enable_runway_in_oc_config(
    bot_id: str, *, model: str = RUNWAY_DEFAULT_MODEL,
) -> tuple[bool, str | None]:
    """Set ``agents.defaults.videoGenerationModel.primary`` in openclaw.json.

    Mirrors telegram_install.enable_channel_in_oc_config — read-merge-
    write via the shared _oc_install_common helpers. Idempotent: an
    already-set primary just gets overwritten to the same value.

    A non-default ``model`` arg lets a future caller pick gen4_turbo
    etc. as the default; v1 always uses gen4.5.
    """
    from . import _oc_install_common as _oc_common

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    vgm = defaults.setdefault("videoGenerationModel", {})
    vgm["primary"] = model

    return _oc_common.write_oc_config(bot_id, cfg)


def disable_runway_in_oc_config(bot_id: str) -> tuple[bool, str | None]:
    """Remove ``videoGenerationModel.primary`` from openclaw.json.

    Idempotent — missing keys treated as success. Does NOT delete the
    whole ``agents.defaults`` block (other defaults like Telegram
    routing may live there).
    """
    from . import _oc_install_common as _oc_common

    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return False, err or "oc_read_failed"

    agents = cfg.get("agents") or {}
    defaults = agents.get("defaults") or {}
    vgm = defaults.get("videoGenerationModel") or {}
    if vgm.get("primary"):
        del vgm["primary"]
    # Clean up empty containers
    if not vgm and "videoGenerationModel" in defaults:
        del defaults["videoGenerationModel"]
    return _oc_common.write_oc_config(bot_id, cfg)


# ── Status resolver (bundled-plugin pattern, distinct from MCP resolvers) ────


def resolve_status_bundled(
    bot_id: str,
    *,
    read_oc_config: Callable,  # (bot_id) -> (dict | None, err)
    read_auth_profiles_fn: Callable | None = None,  # (bot_id) -> dict
) -> InstallStatus:
    """Resolve install status from openclaw.json + auth-profiles.json.

    Active when BOTH:
      * ``agents.defaults.videoGenerationModel.primary`` starts with
        ``runway/`` in openclaw.json
      * ``profiles["runway:default"]`` exists in auth-profiles.json with
        a non-empty ``key`` field

    Status values:
      * ``valid``    — both signals present (install fully wired)
      * ``revoked``  — model-default present but auth profile missing/empty
                       (operator wiped the key but didn't undo the
                       openclaw.json edit)
      * ``invalid``  — auth profile present but model-default not set
                       to runway/* (partial install — key present but
                       OC won't dispatch video gen to runway)
      * ``missing``  — neither signal present
      * ``unknown``  — openclaw.json could not be read
    """
    _read_auth = read_auth_profiles_fn or read_auth_profiles

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

    primary = (
        (oc.get("agents") or {})
        .get("defaults", {})
        .get("videoGenerationModel", {})
        .get("primary") or ""
    )
    model_signal = primary.startswith("runway/")

    try:
        auth = _read_auth(bot_id) or {}
    except Exception:
        auth = {}
    profile = (auth.get("profiles") or {}).get(RUNWAY_AUTH_PROFILE_ID) or {}
    auth_signal = bool(profile.get("key"))

    if model_signal and auth_signal:
        return InstallStatus(
            bot_id=bot_id, status="valid",
        )
    if model_signal and not auth_signal:
        return InstallStatus(
            bot_id=bot_id, status="revoked",
            error="auth_profile_missing_or_empty",
        )
    if auth_signal and not model_signal:
        return InstallStatus(
            bot_id=bot_id, status="invalid",
            error="model_default_not_runway",
        )
    return InstallStatus(bot_id=bot_id, status="missing")
