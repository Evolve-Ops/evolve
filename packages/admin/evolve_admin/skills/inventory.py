"""evolve_admin.skills — per-bot and pod-level skills inventory.

Spec: evolve-mvp-sprint.md §12 (Skills inventory view).

Skills in the Evolve model are capability primitives — things a bot CAN do
(email, calendar, memory, search, messaging). They divide into two categories:

  OpenClaw plugins  — installed via ``openclaw plugins install``; stored in
                      openclaw.json → plugins.entries. These are OpenClaw-
                      proprietary SKILL.md format.

  MCP servers       — installed via mcp.servers block of openclaw.json. These
                      are agentskills.io / MCP-standard and portable across
                      runtimes (per substrate-strategy memory).

Status values:
  configured        — plugin is enabled; treat as operational (credentials are
                      stored in env, auth.profiles, or channels.* — not in
                      plugins.entries[].config for most plugin types)
  needs_oauth       — OAuth-based provider where OpenClaw exposes an explicit
                      OAuth grant block (google_workspace only at v1)
  missing_config    — plugin is disabled and has no visible credential config

Format compliance:
  standard          — MCP-protocol server; agentskills.io-aligned
  proprietary       — OpenClaw-only plugin (SKILL.md format)

Design note: the status heuristic uses ``enabled`` as the primary signal.
Credential verification at call time is handled by OpenClaw itself; attempting
to snoop for API keys in plugins.entries[].config produces false negatives
because LLM providers (anthropic, google, openai, xai) store keys in env or
auth.profiles, and channel plugins (telegram, slack, discord) store tokens in
channels.* — not in plugins.entries[].config.

The ``get_bot_skills`` function returns a SkillInventory for one bot.
The ``get_pod_skills`` function rolls up all bots into a matrix.
"""

from __future__ import annotations

import json
import pwd as _pwd
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ── Known plugin → display mapping ───────────────────────────────────────────
# Maps openclaw plugin entry names to human-readable display names and skill
# categories. Extend as new first-party / commonly-seen plugins appear.
_PLUGIN_DISPLAY: dict[str, dict[str, str]] = {
    "evolve": {"display": "Evolve (pod ops)", "category": "ops"},
    "slack": {"display": "Slack", "category": "messaging"},
    "telegram": {"display": "Telegram", "category": "messaging"},
    "discord": {"display": "Discord", "category": "messaging"},
    # imessage + whatsapp added 2026-06-04 (Phase 1.1 + 1.2 of the
    # OpenClaw coverage audit). Both are wired through OC's bundled or
    # clawhub-shipped channel plugins; install modules in skills/.
    "imessage": {"display": "iMessage", "category": "messaging"},
    "whatsapp": {"display": "WhatsApp", "category": "messaging"},
    # signal added 2026-06-04 (Phase 1.3) — LICENSING REVIEW REQUIRED
    # BEFORE MERGE; see signal_install module docstring.
    "signal":   {"display": "Signal",   "category": "messaging"},
    "anthropic": {"display": "Anthropic LLM", "category": "llm"},
    "openai": {"display": "OpenAI", "category": "llm"},
    "google": {"display": "Google (Gemini)", "category": "llm"},
    "xai": {"display": "xAI (Grok)", "category": "llm"},
    "brave": {"display": "Brave Search", "category": "search"},
    # unity display mapping removed 2026-05-30 (Phase 1c withdrawal) —
    # no upstream OC unity plugin exists, `openclaw plugins install unity`
    # is rejected by the CLI, no implementation anywhere for the access
    # panel's promised capabilities. Re-add if/when OC ships a unity
    # extension or a community Unity MCP gets vetted.
    "github": {"display": "GitHub", "category": "tools"},
    "dropbox": {"display": "Dropbox", "category": "storage"},
    "google_workspace": {"display": "Google Workspace (GOG)", "category": "workspace"},
    "obsidian": {"display": "Obsidian Memory", "category": "memory"},
    "linear": {"display": "Linear", "category": "tools"},
    "notion": {"display": "Notion", "category": "storage"},
    # Zoom added 2026-06-06 (Phase 3 of the Zoom skill). Backed by the
    # Evolve-owned evolve-zoom-mcp shim — see zoom_install.py and
    # docs/spec-zoom-skill-2026-06-06.md.
    "zoom": {"display": "Zoom", "category": "tools"},
}

# OAuth-based providers that need an explicit OAuth grant flow (not env / API key).
# NOTE: "google" is NOT in this set — on OpenClaw it is the Gemini LLM provider,
# not Google Workspace. Only google_workspace triggers the OAuth badge.
# dropbox: only relevant when plugins.entries.dropbox exists (OAuth plugin path).
# The canonical desktop-app path is detected via ~/.dropbox/info.json in
# section 6 of get_bot_skills() and never touches _OAUTH_PROVIDERS.
_OAUTH_PROVIDERS: frozenset[str] = frozenset({
    "google_workspace", "dropbox",
})

# Providers whose credential lives INSIDE plugins.entries[<name>].config, so
# "enabled but keyless" is directly detectable. The enabled-only heuristic
# above exists to avoid false negatives for plugins that keep credentials
# elsewhere (env, auth.profiles, channels.*) — that reasoning does not apply
# here, and treating these as "configured" on the strength of `enabled` alone
# is a false POSITIVE: the Skills page vouches for a tool that fails at call
# time. Value is the dotted path within the plugin entry's `config` block.
#
# The provider SET is shared with the Credentials tab's visibility rule
# (web.credentials_oc.INLINE_KEY_PROVIDERS) so the two surfaces cannot drift
# — one page calling brave "configured" while the other calls it a gap is
# the class of mismatch #3219 had to fix. This module additionally records
# each provider's key PATH, which only it needs.
#
# Keep this list narrow. Only add a provider when the key path is stable and
# authoritative — a wrong path here turns a working plugin into a permanent
# "Needs setup" badge, which is the failure mode the enabled-only rule was
# written to prevent.
_INLINE_KEY_PATHS: dict[str, str] = {
    "brave": "webSearch.apiKey",
}


def _inline_key_providers() -> frozenset[str]:
    """The shared provider set, with a local fallback.

    Imported lazily so ``skills.inventory`` (used by CLI paths that never
    touch Flask) doesn't hard-depend on the web package at module import.
    """
    try:
        from ..web.credentials_oc import INLINE_KEY_PROVIDERS
        return INLINE_KEY_PROVIDERS
    except Exception:  # noqa: BLE001
        return frozenset(_INLINE_KEY_PATHS)


def _inline_key_present(name: str, cfg: dict[str, Any], oc: dict[str, Any]) -> bool:
    """True if an ``_INLINE_KEY_PROVIDERS`` plugin has its credential set.

    Brave delegates to ``brave_key_from_oc_config`` so this agrees with the
    Credentials tab exactly — that helper also honours the LEGACY
    ``tools.web.search.apiKey`` location, and a bot configured that way has
    working search. Disagreeing would re-create the mismatch #3219 fixed,
    just pointed the other way.
    """
    if name == "brave":
        try:
            from ..web.credentials_oc import brave_key_from_oc_config
            return bool(brave_key_from_oc_config(oc))
        except Exception:  # noqa: BLE001 — never fail inventory on detection
            return True  # unknown → don't cry wolf

    path = _INLINE_KEY_PATHS.get(name)
    if not path:
        return True
    cursor: Any = cfg.get("config") or {}
    for segment in path.split("."):
        if not isinstance(cursor, dict):
            return False
        cursor = cursor.get(segment)
    return bool(str(cursor or "").strip())

# Filesystem-based skills that live outside openclaw.json's plugins/MCP blocks.
# These are discovered by checking for a per-skill config file at:
#   ~/.openclaw/skills/<skill_id>.json
# Each entry carries display info (mirrors _PLUGIN_DISPLAY shape) plus the
# config-key name whose presence/non-emptiness means "this skill is active
# enough to use".
#
# Withdrawn 2026-05-30: obsidian_vault, home_assistant, notion, linear, runway.
# Those five were paste-token install flows that wrote a credential file the
# inventory then detected as "configured" — but no code anywhere in the
# codebase consumed the file at runtime. The user would paste a key, the
# Skills tab would render ✓, and the bot still couldn't actually use the
# skill. They are coming back as MCP server installs once vetted; see
# docs/design/paste-token-skills-future-2026-05-30.md and
# docs/audit-skills-install-flows-2026-05-30.md.
#
# Telegram stays here because it has a real runtime consumer (the OC telegram
# plugin loads from channels.telegram, which the install flow writes
# alongside the filesystem marker via telegram_install.enable_channel_in_oc_config).
_FILESYSTEM_SKILLS: dict[str, dict[str, str]] = {
    # Telegram — BotFather token paste via /api/skills/install/telegram/set-token.
    # Config file: ~/.openclaw/skills/telegram.json with {bot_token, bot_username,
    # bot_first_name, can_join_groups, can_read_all_group_messages, verified_at}.
    #
    # Telegram is ALSO listed in _CHANNEL_BACKED_SKILLS for legacy bots that have
    # their token under oc.channels.telegram instead of the filesystem file. The
    # de-dup at line ~434 prevents double-registration: filesystem path wins when
    # both exist, channels path fills in when only it exists.
    "telegram": {
        "display": "Telegram", "category": "messaging",
        "active_field": "bot_token",
    },
}

# Channels-backed messaging skills — bots can be configured for these via
# channels.<provider>.<bot_id> blocks in openclaw.json (e.g. personal_bot has
# channels.telegram but no plugins.entries.telegram and the bot still
# receives Telegram messages). The plugins iteration below would miss these,
# so we explicitly check the channels block for known messaging providers
# and surface them in the inventory under the same skill id as the plugin
# would use. Status: "configured" if a meaningful token field is present.
#
# Coverage extended 2026-06-04 to include every OpenClaw-bundled or officially
# catalogued channel after the OC channel coverage audit
# (docs/openclaw-coverage-audit-2026-06-04.md, PR #2123) caught the inverse-D
# silent-failure mode: an operator who wires any of these channels via
# `openclaw channels add` directly was previously invisible to the Skills
# page. With this passthrough, manually-wired channels surface as
# install_source="channels" + status="configured" so the inventory stops
# silently missing them, even when no Evolve install module has been written
# yet. Full Phase-1 wizards (whatsapp/signal/matrix/imessage rewire) are
# tracked separately; this dict is the safety net that closes the visibility
# gap independent of wizard coverage.
_CHANNEL_BACKED_SKILLS: dict[str, dict[str, Any]] = {
    # ── Wrapped today (have install modules) ─────────────────────────────────
    "slack":    {"display": "Slack",    "category": "messaging",
                 "token_fields": ("botToken", "appToken", "userToken", "access_token", "token")},
    "telegram": {"display": "Telegram", "category": "messaging",
                 "token_fields": ("botToken", "token", "access_token")},
    "discord":  {"display": "Discord",  "category": "messaging",
                 "token_fields": ("botToken", "token", "access_token")},

    # ── Bundled OC channels w/o (or pending) Evolve install module ───────────
    # Each is shipped by OC at dist/extensions/<id>/ or dist/channel-catalog.json;
    # an operator running `openclaw channels add --channel <id> ...` produces a
    # channels.<id> block we want surfaced. token_fields are the credential
    # signals from each channel's account config in OC's TypeScript defs
    # (plugin-sdk/types.channels-*.d.ts) — being liberal here is fine because
    # the recursive _has_token scan only matches non-empty string values.
    "whatsapp": {"display": "WhatsApp", "category": "messaging",
                 "token_fields": ("authDir", "defaultTo", "phoneNumber", "name")},
    "signal":   {"display": "Signal",   "category": "messaging",
                 "token_fields": ("number", "account", "username", "phoneNumber")},
    "matrix":   {"display": "Matrix",   "category": "messaging",
                 "token_fields": ("accessToken", "userId", "homeserver", "token")},
    "imessage": {"display": "iMessage", "category": "messaging",
                 "token_fields": ("handle", "dbPath", "service")},
    "mattermost": {"display": "Mattermost", "category": "messaging",
                   "token_fields": ("token", "personalAccessToken", "serverUrl", "accessToken")},
    "sms":      {"display": "SMS (Twilio)", "category": "messaging",
                 "token_fields": ("accountSid", "authToken", "phoneNumber", "fromNumber")},
    "irc":      {"display": "IRC",      "category": "messaging",
                 "token_fields": ("server", "nickname", "nick", "saslPassword", "password")},
    # Officially catalogued (npm/clawhub) channels not bundled in core; cheap
    # to include in the inventory passthrough — the dict only matters if a
    # channels.<id> block actually exists on a bot.
    "googlechat":     {"display": "Google Chat",     "category": "messaging",
                       "token_fields": ("webhookUrl", "webhookPath", "audienceValue", "token")},
    "msteams":        {"display": "Microsoft Teams", "category": "messaging",
                       "token_fields": ("appId", "appPassword", "token", "tenantId")},
    "line":           {"display": "LINE",            "category": "messaging",
                       "token_fields": ("channelAccessToken", "channelSecret", "token")},
    "feishu":         {"display": "Feishu/Lark",     "category": "messaging",
                       "token_fields": ("appId", "appSecret", "verificationToken", "token")},
    "nostr":          {"display": "Nostr",           "category": "messaging",
                       "token_fields": ("privkey", "relays", "npub", "nsec")},
    "synology-chat":  {"display": "Synology Chat",   "category": "messaging",
                       "token_fields": ("webhookUrl", "incomingToken", "token")},
    "nextcloud-talk": {"display": "Nextcloud Talk",  "category": "messaging",
                       "token_fields": ("webhookUrl", "serverUrl", "token", "appPassword")},
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class SkillEntry:
    """One skill (plugin or MCP server) installed on a bot."""

    # Stable identifier — plugin name or MCP server name
    id: str
    # Human-readable name for the UI
    display: str
    # Category bucket: messaging | llm | search | workspace | memory | tools | storage | ops | mcp
    category: str
    # "configured" | "needs_oauth" | "missing_config"
    status: str
    # "standard" (MCP/agentskills.io) or "proprietary" (OpenClaw plugin)
    format_compliance: str
    # Whether the plugin is currently enabled in openclaw.json (plugins only)
    enabled: bool = True
    # Install source when known (from plugins.installs[name].source)
    install_source: str | None = None
    # Names of applications (manifests) that declare this skill as a requirement
    apps_using: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class SkillInventory:
    """All skills on one bot."""

    bot_id: str
    skills: list[SkillEntry] = field(default_factory=list)
    # Error reading openclaw.json, if any
    read_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skills": [s.to_dict() for s in self.skills],
            "read_error": self.read_error,
        }


# ── openclaw.json reader ──────────────────────────────────────────────────────

def _read_oc_json(bot_home: Path) -> tuple[dict | None, str | None]:
    """Read openclaw.json. Direct first, sudo /bin/cat fallback."""
    path = bot_home / ".openclaw" / "openclaw.json"
    try:
        text = path.read_text()
        return json.loads(text), None
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        return None, str(e)

    # Fallback: sudo /bin/cat as root
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout), None
        return None, f"sudo_rc={r.returncode}"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


# ── Auth status helpers ───────────────────────────────────────────────────────

def _resolve_plugin_status(
    name: str,
    cfg: dict[str, Any],
    oc: dict[str, Any],
) -> str:
    """Determine configured/needs_oauth/missing_config for a plugin.

    Strategy (v1):
    - Primary signal: ``enabled`` flag in the plugin entry.
      - enabled=True  → "configured" (OpenClaw enforces credentials at call time;
        they live in env, auth.profiles, or channels.*, not plugins.entries[].config
        for most plugin types including all LLM providers and channel plugins).
      - enabled=False → "missing_config" (plugin is off; no useful status).
    - Exception: OAuth-only providers in _OAUTH_PROVIDERS without an explicit
      auth block → "needs_oauth" (e.g. google_workspace, dropbox).

    This avoids false "missing_config" for plugins like anthropic, google,
    openai, xai (which store API keys in env / auth.profiles) and telegram,
    slack, discord (which store tokens in channels.* not plugins.entries[].config).

    - Exception 2: providers in ``_INLINE_KEY_PROVIDERS`` keep their credential
      INSIDE ``plugins.entries[].config``, so the false-negative risk the
      ``enabled``-only rule protects against doesn't apply — the key is right
      there to check. Enabled + no key → "missing_config".

      Brave is why this exists. Reading ``enabled`` alone reported "installed
      on all bots" for a fleet where 6 of 9 had no API key, so the Skills page
      vouched for a web_search tool that 401s at call time. An ``enabled`` flag
      is a capability claim; for these providers we can actually verify it.
    """
    enabled = cfg.get("enabled") is not False  # None → enabled by default

    # Credential lives in plugins.entries[].config → verify it rather than
    # trusting `enabled`. Only for providers where we KNOW the key path;
    # everything else keeps the conservative enabled-only rule.
    if enabled and name.lower() in _inline_key_providers():
        if not _inline_key_present(name.lower(), cfg, oc):
            return "missing_config"

    # OAuth-based providers that require an explicit grant flow:
    # Show needs_oauth only when enabled (otherwise disabled is more accurate).
    if name.lower() in _OAUTH_PROVIDERS and enabled:
        # Check if an OAuth grant block exists in the config
        plugin_config = cfg.get("config") or {}
        auth_block = oc.get("auth") or {}
        # If there's any auth profile for this provider, consider it configured.
        profiles = auth_block.get("profiles") or {}
        has_auth = any(k.startswith(f"{name}:") for k in profiles)
        if not has_auth:
            return "needs_oauth"

    if enabled:
        return "configured"

    return "missing_config"


def _resolve_mcp_status(server: dict[str, Any]) -> str:
    """Status for an MCP server.

    MCP servers declared in openclaw.json are considered configured unless they
    reference environment variables that look like placeholders (e.g.
    $SOME_TOKEN_PLACEHOLDER). Env vars that have real-looking values → configured.
    """
    env = server.get("env") or {}
    if not env:
        # No env vars required → probably configured (command-only server)
        return "configured"

    # If any env var looks like a placeholder (starts with $ or is empty), flag it
    for k, v in env.items():
        if not v or (isinstance(v, str) and (v.startswith("$") or not v.strip())):
            return "missing_config"

    return "configured"


# ── Manifest app-dependency reader ───────────────────────────────────────────

def _read_app_skill_deps(bot_id: str, bot_user: str) -> dict[str, list[str]]:
    """Return {skill_id: [app_name, ...]} from manifests' requirements.integrations.

    Reads manifests from the bot's workspace. Skill IDs are matched against
    the integration id field in requirements.integrations[].
    """
    try:
        home = _pwd.getpwnam(bot_user).pw_dir
    except KeyError:
        home = f"/Users/{bot_user}"

    # Try the standard workspace manifest dirs
    manifest_candidates = [
        Path(f"/Users/Shared/evolve/{bot_id}/manifests"),
        Path(home) / ".openclaw" / "workspace" / "manifests",
    ]

    skill_to_apps: dict[str, list[str]] = {}

    for mdir in manifest_candidates:
        if not mdir.exists():
            continue
        try:
            files = list(mdir.glob("*.json"))
        except (PermissionError, OSError):
            continue
        for fpath in files:
            if "_history" in fpath.name or fpath.name.startswith("."):
                continue
            try:
                data = json.loads(fpath.read_text())
            except Exception:
                continue
            app_name = data.get("display_name") or data.get("name") or fpath.stem
            reqs = data.get("requirements") or {}
            for integ in reqs.get("integrations") or []:
                integ_id = integ.get("id") or ""
                if integ_id:
                    skill_to_apps.setdefault(integ_id, [])
                    if app_name not in skill_to_apps[integ_id]:
                        skill_to_apps[integ_id].append(app_name)
        # Stop at first directory that had manifest files
        if skill_to_apps:
            break

    return skill_to_apps


# ── Core inventory builder ────────────────────────────────────────────────────

def get_bot_skills(
    bot_id: str,
    bot_user: str,
    network: "dict[str, Any] | None" = None,
) -> SkillInventory:
    """Build a SkillInventory for one bot.

    bot_id   — logical name (key in network.json)
    bot_user — actual macOS username for filesystem access
    network  — parsed network.json (optional; used for future enrichment)
    """
    try:
        home = Path(_pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")

    oc, err = _read_oc_json(home)
    if oc is None:
        return SkillInventory(bot_id=bot_id, read_error=err or "unknown")

    skills: list[SkillEntry] = []

    # ── 1. OpenClaw plugins ───────────────────────────────────────────────────
    plugins_block = oc.get("plugins") or {}
    entries_block = plugins_block.get("entries") or {}
    installs_block = plugins_block.get("installs") or {}

    for name, cfg in entries_block.items():
        if not isinstance(cfg, dict):
            continue
        display_info = _PLUGIN_DISPLAY.get(name) or {
            "display": name.replace("_", " ").title(),
            "category": "tools",
        }
        enabled = cfg.get("enabled") is not False  # None or True → enabled
        install_info = installs_block.get(name) or {}
        install_source = (
            install_info.get("source") if isinstance(install_info, dict) else None
        )
        status = _resolve_plugin_status(name, cfg, oc)
        skills.append(SkillEntry(
            id=name,
            display=display_info["display"],
            category=display_info["category"],
            status=status,
            format_compliance="proprietary",
            enabled=enabled,
            install_source=install_source,
        ))

    # ── 2. MCP servers ────────────────────────────────────────────────────────
    mcp_block = oc.get("mcp") or {}
    servers_block = mcp_block.get("servers") or {}

    for name, server_cfg in servers_block.items():
        if not isinstance(server_cfg, dict):
            continue
        # MCP server names often look like "brave-search", "github-mcp", etc.
        display_info = _PLUGIN_DISPLAY.get(name) or {
            "display": name.replace("-", " ").replace("_", " ").title(),
            "category": "mcp",
        }
        status = _resolve_mcp_status(server_cfg)
        skills.append(SkillEntry(
            id=f"mcp:{name}",
            display=display_info["display"] + " (MCP)",
            category=display_info["category"],
            status=status,
            format_compliance="standard",
            enabled=True,
            install_source="mcp",
        ))

    # ── 3. Filesystem skills (Obsidian, Home Assistant, Notion) ──────────────
    # Filesystem skills are not in plugins.entries or mcp.servers; they live
    # in ~/.openclaw/skills/<skill_id>.json (written by the install flow).
    skills_config_dir = home / ".openclaw" / "skills"
    for fs_skill_id, fs_display_info in _FILESYSTEM_SKILLS.items():
        config_path = skills_config_dir / f"{fs_skill_id}.json"
        if not config_path.exists():
            # Absent config → skill not installed for this bot → skip.
            continue
        try:
            fs_config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            fs_config = {}
        active_field = fs_display_info.get("active_field") or "access_token"
        active_value = (fs_config or {}).get(active_field) or None
        fs_status = "configured" if active_value else "missing_config"
        skills.append(SkillEntry(
            id=fs_skill_id,
            display=fs_display_info["display"],
            category=fs_display_info["category"],
            status=fs_status,
            format_compliance="filesystem",
            enabled=bool(active_value),
            install_source="filesystem",
        ))

    # ── 4. Channels-backed messaging skills ──────────────────────────────────
    # Bots can be configured for slack/telegram/discord via channels.<provider>
    # without a corresponding plugins.entries.<provider>. The plugins iteration
    # above would miss those bots; we explicitly check the channels block here
    # to make the inventory tell the truth (per CLAUDE.md: personal_bot has
    # channels.telegram but no plugins.entries.telegram and Telegram works).
    existing_ids = {s.id for s in skills}
    channels_block = oc.get("channels") or {}
    for ch_skill_id, ch_info in _CHANNEL_BACKED_SKILLS.items():
        if ch_skill_id in existing_ids:
            # Plugin entry already covered it — don't double-register.
            continue
        ch_cfg = channels_block.get(ch_skill_id)
        if not isinstance(ch_cfg, dict) or not ch_cfg:
            continue

        # Token presence: scan the entire channel config (which may be nested
        # by bot_id under the provider key) for any of the known token fields
        # with a non-empty string value.
        def _has_token(node: Any) -> bool:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ch_info["token_fields"] and isinstance(v, str) and v.strip():
                        return True
                    if _has_token(v):
                        return True
            elif isinstance(node, list):
                return any(_has_token(x) for x in node)
            return False

        status = "configured" if _has_token(ch_cfg) else "missing_config"
        skills.append(SkillEntry(
            id=ch_skill_id,
            display=ch_info["display"],
            category=ch_info["category"],
            status=status,
            format_compliance="proprietary",
            enabled=status == "configured",
            install_source="channels",
        ))

    # ── 5. GitHub — .git/config supplemental detection ───────────────────────
    # GitHub credentials live in the workspace .git/config remote URL (PAT
    # embedded as https://<token>@github.com/...) or as SSH keys, not in
    # plugins.entries. If plugins.entries already covered it, skip.
    if "github" not in existing_ids:
        _workspace = (
            ((oc.get("agents") or {}).get("defaults") or {}).get("workspace")
            or str(home / ".openclaw" / "workspace")
        )
        _git_cfg = Path(_workspace) / ".git" / "config"
        _git_text: str | None = None
        try:
            _git_text = _git_cfg.read_text()
        except FileNotFoundError:
            pass
        except PermissionError:
            try:
                _r = subprocess.run(
                    ["sudo", "/bin/cat", str(_git_cfg)],
                    capture_output=True, text=True, timeout=5,
                )
                if _r.returncode == 0:
                    _git_text = _r.stdout
            except Exception:
                pass
        except Exception:
            pass
        if _git_text and "github.com" in _git_text:
            skills.append(SkillEntry(
                id="github",
                display="GitHub",
                category="tools",
                status="configured",
                format_compliance="proprietary",
                enabled=True,
                install_source="git_config",
            ))
            existing_ids.add("github")

    # §6 (Dropbox via ~/.dropbox/info.json) was removed 2026-05-30 alongside
    # the dropbox MCP-install rewire. The old detection was a false positive:
    # presence of info.json only proved the desktop client was installed for
    # the bot's macOS account, not that any agent had access. A 2026-05-29
    # plugins-page audit flagged this on several bots.
    # Dropbox is now an MCP-backed install (mcp.servers.dropbox via the
    # filesystem catalog entry) and surfaces through §2 detection above.

    # ── 7. App-skill linkages ─────────────────────────────────────────────────
    try:
        skill_to_apps = _read_app_skill_deps(bot_id, bot_user)
    except Exception:
        skill_to_apps = {}

    for skill in skills:
        # Match by plugin name or mcp server name (strip "mcp:" prefix for MCP)
        raw_id = skill.id[4:] if skill.id.startswith("mcp:") else skill.id
        apps = skill_to_apps.get(raw_id) or skill_to_apps.get(skill.id) or []
        skill.apps_using = apps

    # Sort for stable output: by category then display name
    skills.sort(key=lambda s: (s.category, s.display.lower()))

    return SkillInventory(bot_id=bot_id, skills=skills)


# ── Pod-wide local-system + catalog-stub skills (P2 from skills roadmap) ─────
#
# Some skills aren't well-served by per-bot inventory detection:
#
#  * **apple_local** is a local_system skill — its install state IS the macOS
#    TCC grant state for whichever user the AppleScript probe runs as. Running
#    the probe per-bot would call osascript ~4 × N_bots times per matrix
#    refresh (~5s per probe, capped by the timeout). Worse, the probe runs
#    as the admin server's `evolve` user, not the bot's user, so the result
#    isn't truly per-bot anyway — TCC grants are user-scoped, not bot-scoped.
#
#  * **autocad** ships as a catalog stub today — its resolve_status always
#    returns "needs_app" until the OAuth installer ships in a follow-up PR.
#    No per-bot state worth checking; one call returns the answer for everyone.
#
# Both get added to the matrix via :func:`get_pod_skills` instead of
# :func:`get_bot_skills` — we resolve their status ONCE per matrix refresh and
# apply the result to every bot's inventory entry. The status semantics are
# "pod-wide approximation"; users wanting per-bot precision should click into
# the per-skill status endpoint (which runs the live probe each call).
#
# Adding new pod-wide-local skills: append a row to _POD_WIDE_LOCAL_SKILLS
# below. The resolver is called once per get_pod_skills; the SkillEntry is
# attached to every bot's matrix cell with the same status.

_POD_WIDE_LOCAL_SKILLS: tuple[tuple[str, str, str], ...] = (
    # (skill_id, display, category) — resolvers are imported lazily so this
    # module stays importable in test environments that stub out skills.
    #
    # apple_local WITHDRAWN 2026-05-30 (Phase 1c of the deep skills audit;
    # see docs/skills-deep-audit-2026-05-30.md). The probe-as-only-consumer
    # dead-end: TCC grants for the `evolve` admin user don't help bots that
    # run as team_bot_a/team_bot_c/personal_bot_user/etc., and OC has no Contacts/Calendar/
    # Reminders/Notes plugin or tool surface anywhere. Removed from the
    # pod-wide matrix so green "active" chips don't lie. Re-add when an
    # apple-mcp-server lands or osascript tool surfaces ship in
    # packages/plugin/src.
    ("autocad", "AutoCAD (APS)", "tools"),
)


def _resolve_apple_local_status() -> str:
    """Return the matrix status for apple_local — runs the TCC probe once.

    Returns ``active`` (all four Apple apps grant TCC to the probing user),
    ``needs_tcc`` (one or more denied), or ``unknown`` (probe error).
    Mirrors the apple_local_install.InstallStatus.status field but as a bare
    string for matrix consumption.
    """
    try:
        from . import apple_local_install as _apple
        st = _apple.resolve_status(bot_id="_pod_wide_probe")
        return st.status
    except Exception:
        return "unknown"


def _resolve_autocad_status() -> str:
    """Return the matrix status for autocad. Always ``needs_app`` in v1."""
    try:
        from . import autocad_install as _autocad
        st = _autocad.resolve_status(bot_id="_pod_wide_probe")
        return st.status
    except Exception:
        return "unknown"


# ── Pod-level rollup ──────────────────────────────────────────────────────────

def get_pod_skills(
    bots: dict[str, dict[str, Any]],
    network: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Roll up skill inventories across all bots.

    Returns a dict shaped for the /api/skills/pod endpoint:
    {
      "bots": {bot_id: SkillInventory.to_dict()},
      "matrix": {skill_id: {bot_id: status | None}},
      "skill_meta": {skill_id: {display, category, format_compliance}},
    }

    The matrix lets the UI answer "GOG is on team_bot_a and admin_bot, but not team_bot_b".
    """
    # Read each bot's inventory in parallel. Sequential reads make the
    # /api/skills/pod call O(N_bots × per-bot-latency) — a single bot whose
    # ~/.openclaw/openclaw.json triggers the sudo /bin/cat fallback (5s
    # timeout) used to stall the whole pod load, leaving the "Across pod"
    # tab on a Loading… spinner for tens of seconds. ThreadPool keeps wall
    # time at max-per-bot rather than sum.
    from concurrent.futures import ThreadPoolExecutor

    def _load_one(item: tuple[str, "dict | None"]) -> tuple[str, SkillInventory]:
        bid, bcfg = item
        buser = (bcfg or {}).get("user") or bid
        try:
            return bid, get_bot_skills(bid, buser, network)
        except Exception as exc:
            return bid, SkillInventory(
                bot_id=bid, read_error=f"{exc.__class__.__name__}: {exc}",
            )

    items = list(bots.items())
    inventories: dict[str, SkillInventory] = {}
    if items:
        # max_workers caps at 8 to bound contention on the sudo /bin/cat
        # subprocess pool; pods larger than 8 bots still amortize well.
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
            for bid, inv in ex.map(_load_one, items):
                inventories[bid] = inv

    # Build the cross-bot skill matrix
    # skill_id → {bot_id: status}
    matrix: dict[str, dict[str, str | None]] = {}
    skill_meta: dict[str, dict[str, str]] = {}
    all_bot_ids = list(bots.keys())

    for bot_id, inv in inventories.items():
        for skill in inv.skills:
            if skill.id not in matrix:
                matrix[skill.id] = {bid: None for bid in all_bot_ids}
                skill_meta[skill.id] = {
                    "display": skill.display,
                    "category": skill.category,
                    "format_compliance": skill.format_compliance,
                }
            matrix[skill.id][bot_id] = skill.status

    # P2: pod-wide catalog-stub skills (autocad). Resolve once and apply
    # to every bot. The per-skill status endpoint still does the live
    # per-call probe for users who want precise per-bot state. See
    # _POD_WIDE_LOCAL_SKILLS above.
    #
    # apple_local was here too pre-2026-05-30; withdrawn in Phase 1c of
    # the deep skills audit. _resolve_apple_local_status is kept on the
    # module for now in case the per-skill status endpoint dispatch
    # (server.py — which I'm also removing) still has stragglers.
    _pod_wide_resolvers = {
        "autocad": _resolve_autocad_status,
    }
    for skill_id, display, category in _POD_WIDE_LOCAL_SKILLS:
        # Skip if a bot's per-bot inventory already surfaced this skill
        # (defensive — shouldn't happen today since neither skill writes a
        # plugin / mcp.servers / channels entry, but future evolutions might).
        if skill_id in matrix:
            continue
        try:
            status = _pod_wide_resolvers[skill_id]()
        except Exception:
            status = "unknown"
        matrix[skill_id] = {bid: status for bid in all_bot_ids}
        skill_meta[skill_id] = {
            "display": display,
            "category": category,
            # local_system / catalog_stub — neither is a true MCP/OC plugin
            # but both are first-party Evolve-managed installs.
            "format_compliance": "local",
        }

    # Sort matrix keys by category + display for stable ordering
    sorted_skill_ids = sorted(
        matrix.keys(),
        key=lambda sid: (
            skill_meta[sid]["category"],
            skill_meta[sid]["display"].lower(),
        ),
    )

    return {
        "bots": {bid: inv.to_dict() for bid, inv in inventories.items()},
        "matrix": {sid: matrix[sid] for sid in sorted_skill_ids},
        "skill_meta": skill_meta,
        "all_bot_ids": all_bot_ids,
    }
