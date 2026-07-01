"""evolve_admin.skills.upstream_plugin_skills — catalog wrappers for skills
that ride on upstream OpenClaw plugins already in use across the pod.

These are skills the pod already supports today via standard OpenClaw plugin
machinery (Brave Search, GitHub, Dropbox) or via existing OAuth profiles
piggybacked off another installer (Google Drive over the GOG profile).
They were missing from the admin Skills catalog so users had no way to
*discover* or *check the install state of* these capabilities even when
running bots already had them turned on.

This module is intentionally lightweight — one registry entry plus a status
resolver per skill. We do not own the install mechanics here: enabling the
underlying OpenClaw plugin or completing OAuth is handled by deploy/wizard
flows. The install POST short-circuits to "already_active" for bots that
have the plugin enabled, and returns a small step-plan with guidance text
for bots that don't.

Auth-profile-aware skills (Dropbox, Drive) additionally check
``openclaw.json -> auth.profiles`` for a profile keyed
``<provider>:<bot_id>`` to distinguish "plugin enabled but never signed in"
from "fully active".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import inventory as _inv


# ── Skill ids ─────────────────────────────────────────────────────────────────

BRAVE_SKILL_ID    = "brave"
GITHUB_SKILL_ID   = "github"
GDRIVE_SKILL_ID   = "gdrive"
DROPBOX_SKILL_ID  = "dropbox"
UNITY_SKILL_ID    = "unity"


# ── Access panels (Plex-test plain language) ─────────────────────────────────

BRAVE_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": BRAVE_SKILL_ID,
    "skill_display_name": "Brave Search",
    "summary": (
        "Lets this bot search the web with Brave Search. Useful for answering "
        "questions that need fresh information from outside the pod."
    ),
    "will": [
        "Search the web on your behalf using Brave",
        "Read public web pages returned by search",
        "Use search results to ground its answers",
    ],
    "wont": [
        "Browse private or password-protected sites",
        "Submit forms or interact with sites it finds",
        "Store your queries anywhere outside this bot",
    ],
    "where_credentials_live": (
        "Brave Search is a pod-wide capability included in every bot's setup. "
        "There is no per-bot token to manage."
    ),
}

GITHUB_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GITHUB_SKILL_ID,
    "skill_display_name": "GitHub",
    "summary": (
        "Backs this bot up to its own private GitHub repository — the bot's "
        "workspace (memory, files, configuration) is pushed there nightly so "
        "you can restore it if the host machine goes away."
    ),
    "will": [
        "Push this bot's workspace to a private GitHub repo on a schedule",
        "Restore the bot's workspace from that repo if the host disappears",
        "Keep the access token on this bot's user account only, not centralised",
    ],
    "wont": [
        "Read or modify any other repository in your GitHub account",
        "Give the bot LLM-driven GitHub access (no issues / PR browsing here "
        "— for that, install the GitHub MCP server from the MCP Servers tab)",
        "Share your token outside this bot",
    ],
    "where_credentials_live": (
        "The personal access token is embedded in this bot's workspace git "
        "remote URL at ~/.openclaw/workspace/.git/config — written by the "
        "Backup page's GitHub onboarding flow. Revoke at any time from "
        "github.com/settings/tokens."
    ),
}

GDRIVE_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": GDRIVE_SKILL_ID,
    "skill_display_name": "Google Drive",
    "summary": (
        "Lets this bot read files in your Google Drive. Useful for pulling "
        "docs into briefings, summarising spreadsheets, and finding files "
        "by name."
    ),
    "will": [
        "List files and folders in your Drive",
        "Read the contents of docs, sheets, and PDFs you ask about",
        "Search Drive for files by name or contents",
    ],
    "wont": [
        "Delete or move files without your explicit confirmation",
        "Share files with anyone outside this bot",
        "Access Drives belonging to other Google accounts",
    ],
    "where_credentials_live": (
        "Drive uses the same Google sign-in as Gmail/Calendar — once you've "
        "set up Google Workspace for this bot, Drive is available too. "
        "Revoke at any time from your Google Account → Security → Third-party access."
    ),
}

UNITY_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": UNITY_SKILL_ID,
    "skill_display_name": "Unity",
    "summary": (
        "Lets this bot interact with the Unity Editor and your Unity "
        "projects — query scene contents, inspect game objects, and run "
        "scripted actions in your open project."
    ),
    "will": [
        "List scenes, prefabs, and assets in your open Unity project",
        "Inspect game objects, components, and their properties",
        "Run scripted Unity Editor actions you ask it to",
    ],
    "wont": [
        "Modify your project without your explicit confirmation",
        "Build, publish, or ship your project on its own",
        "Touch Unity Cloud projects under other accounts",
    ],
    "where_credentials_live": (
        "Unity is a pod-wide capability included in a bot's plugin set. "
        "There is no per-bot token to manage; the bot talks to the locally "
        "running Unity Editor via OpenClaw's Unity plugin."
    ),
}

DROPBOX_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": DROPBOX_SKILL_ID,
    "skill_display_name": "Dropbox",
    "summary": (
        "Lets this bot read files in your Dropbox. Useful for pulling shared "
        "documents, photos, and project folders into the bot's context."
    ),
    "will": [
        "List files and folders in your Dropbox",
        "Read the contents of files you ask about",
        "Search Dropbox for files by name",
    ],
    "wont": [
        "Delete or move files without your explicit confirmation",
        "Share files with anyone outside this bot",
        "Access Dropboxes belonging to other accounts",
    ],
    "where_credentials_live": (
        "Your Dropbox sign-in is stored only on this bot's user account on "
        "your machine. Revoke at any time from dropbox.com → Settings → "
        "Connected apps."
    ),
}


# ── Skill spec ────────────────────────────────────────────────────────────────

@dataclass
class UpstreamPluginSkill:
    """Registry entry for a skill backed by an upstream OpenClaw plugin.

    plugin_name      — key under ``plugins.entries`` to check for enabled state.
                       For ``gdrive``, set this to the GOG plugin (``google_workspace``)
                       since Drive piggybacks on that OAuth.
    auth_profile_key — provider key under ``auth.profiles`` (e.g. "dropbox",
                       "google_workspace"). None means the skill needs no OAuth
                       profile (Brave, GitHub-via-PAT-env).
    install_hint     — short label shown when the skill is not yet active on
                       a bot. The modal renders this as the single install
                       step's label.
    """

    id: str
    display_name: str
    summary: str
    access_panel: dict[str, Any]
    plugin_name: str
    auth_profile_key: str | None
    install_hint: str


SKILLS: dict[str, UpstreamPluginSkill] = {
    BRAVE_SKILL_ID: UpstreamPluginSkill(
        id=BRAVE_SKILL_ID,
        display_name=BRAVE_ACCESS_PANEL["skill_display_name"],
        summary=BRAVE_ACCESS_PANEL["summary"],
        access_panel=BRAVE_ACCESS_PANEL,
        plugin_name="brave",
        auth_profile_key=None,
        install_hint=(
            "Brave Search is added to every bot by the deploy step. If it's "
            "missing here, run `sudo evolve-admin deploy <bot>` to restore it."
        ),
    ),
    GITHUB_SKILL_ID: UpstreamPluginSkill(
        id=GITHUB_SKILL_ID,
        display_name=GITHUB_ACCESS_PANEL["skill_display_name"],
        summary=GITHUB_ACCESS_PANEL["summary"],
        access_panel=GITHUB_ACCESS_PANEL,
        # NOTE: github does NOT have an OpenClaw plugin (no @openclaw/github-plugin
        # on npm). It also doesn't ride on auth.profiles. Status is resolved by
        # the custom resolver below (`_resolve_github_status`) which checks the
        # workspace .git/config for a github remote — the actual purpose-1
        # (backup) wiring. The plugin_name field is kept for the dataclass
        # contract but unused for github.
        #
        # install_hint is also unused for github: build_install_plan
        # short-circuits to a single open_github_backup_wizard step before
        # reaching the manual_setup branch that would render install_hint as
        # its label. See the `skill.id == GITHUB_SKILL_ID` branch in
        # build_install_plan below. The field is kept for the dataclass
        # contract; the marker string makes the unused-ness obvious if
        # something ever ends up reading it.
        plugin_name="github",
        auth_profile_key=None,
        install_hint="(unused — github short-circuits to open_github_backup_wizard; see build_install_plan)",
    ),
    # GDRIVE_SKILL_ID and UNITY_SKILL_ID were previously catalog entries.
    # WITHDRAWN 2026-05-30 (Phase 1c of the deep skills audit) — see
    # docs/skills-deep-audit-2026-05-30.md:
    #
    #   gdrive — no OC runtime Drive consumer exists. OC's bundled `google`
    #     extension is the Gemini LLM provider only (manifest declares
    #     providers ['google','google-gemini-cli','google-vertex']; no
    #     Gmail/Calendar/Drive contract). gdrive piggybacked on the GOG
    #     OAuth profile but GOG_DEFAULT_SERVICES = ('gmail_readonly',
    #     'calendar_readonly') so Drive scopes were never even granted.
    #     resolve_status returned 'active' on any GOG-active bot — guaranteed
    #     false positive. Re-add via real Drive MCP server (e.g.,
    #     google-drive-mcp) through the InstallMcpServer pipeline.
    #
    #   unity — no upstream OC unity plugin exists. `openclaw plugins
    #     inspect unity` returns "Plugin not found"; `openclaw plugins
    #     search unity` returns "No ClawHub plugins found". Install hint
    #     instructed `openclaw plugins install unity` — a command the CLI
    #     rejects. Access panel promised scene listing / GameObject
    #     inspection / scripted Editor actions, none implemented anywhere.
    #     Revisit if/when OpenClaw ships a unity extension or a community
    #     Unity MCP gets vetted.
    #
    # Constants GDRIVE_SKILL_ID / GDRIVE_ACCESS_PANEL / UNITY_SKILL_ID /
    # UNITY_ACCESS_PANEL are kept above for backward-compat in case
    # anything imports them; only the SKILLS entries are removed so the
    # catalog isn't listed.
    #
    # Dropbox was previously an upstream_plugin_skills entry with a
    # honest-but-dead install_hint ("no agent integration"). Rewired
    # 2026-05-30 into a real MCP-backed install with the same shape
    # as Obsidian — catalog_id="filesystem" + extra_args=[folder_path]
    # + OS-ACL mode toggle. The new install lives at
    # packages/admin/evolve_admin/skills/dropbox_install.py; the
    # constants below (DROPBOX_SKILL_ID / DROPBOX_ACCESS_PANEL) are
    # kept for backward-compat in case anything imports them, but
    # the SKILLS entry is removed so the catalog isn't double-listed.
}


# ── Status resolver ──────────────────────────────────────────────────────────

@dataclass
class InstallStatus:
    """Snapshot of a bot's install state for an upstream-plugin skill.

    State machine:

    * ``active``         — plugin enabled and (if required) an auth profile
                           exists. The bot can use the skill right now.
    * ``needs_auth``     — plugin is enabled but its required auth profile is
                           missing. Apply only when ``auth_profile_key`` is set.
    * ``plugin_disabled``— plugin entry is present in openclaw.json but
                           ``enabled: false``.
    * ``missing``        — no plugin entry. The skill isn't installed on this
                           bot yet (deploy didn't add it, or it was removed).
    * ``unknown``        — could not read openclaw.json.
    """

    bot_id: str
    skill_id: str
    status: str  # active | needs_auth | plugin_disabled | missing | unknown
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": self.skill_id,
            "status": self.status,
            "error": self.error,
        }


def _resolve_github_status(bot_id: str) -> InstallStatus:
    """Custom github resolver — checks workspace .git/config for a github remote.

    Github is not an OpenClaw plugin (no @openclaw/github-plugin on npm — verified
    2026-05-30) and the previous resolver hit plugins.entries.github which was
    never written by any code path, so github always reported "missing" even on
    bots where the backup PAT was set. The actual canonical store for purpose-1
    (self-backup) GitHub credentials is the workspace's .git/config remote URL,
    written by /api/admin/onboard/github. Same detection logic used by
    skills.inventory's §5 supplemental detection.

    States: active (remote present) | missing (no remote) | unknown (read error).
    """
    import re as _re
    import subprocess as _subproc
    from pathlib import Path as _Path

    from ..config import bot_home

    try:
        home = bot_home(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="unknown",
            error=f"bot_home_failed: {exc}",
        )

    # Resolve the workspace path. Default to <home>/.openclaw/workspace, but
    # honour openclaw.json::agents.defaults.workspace if set (some bots
    # relocate it).
    oc, _err = _inv._read_oc_json(home)
    workspace_str = (
        ((oc or {}).get("agents") or {}).get("defaults") or {}
    ).get("workspace") or str(home / ".openclaw" / "workspace")

    git_config = _Path(workspace_str) / ".git" / "config"
    cfg_text: str | None = None
    try:
        cfg_text = git_config.read_text()
    except FileNotFoundError:
        return InstallStatus(
            bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="missing",
        )
    except PermissionError:
        # ACL fallthrough — try sudo /bin/cat (same fallback pattern as
        # skills.inventory and gallery.preflight_check).
        try:
            r = _subproc.run(
                ["sudo", "/bin/cat", str(git_config)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                cfg_text = r.stdout
        except Exception as exc:
            return InstallStatus(
                bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="unknown",
                error=f"git_config_read_failed: {exc.__class__.__name__}",
            )
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="unknown",
            error=f"git_config_read_failed: {exc.__class__.__name__}",
        )

    if not cfg_text or "github.com" not in cfg_text:
        return InstallStatus(
            bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="missing",
        )

    # Active iff at least one remote URL points at github.com AND embeds a
    # token (the bare URL would be present-but-unauthenticated; backup pushes
    # would fail).
    if _re.search(
        r"url\s*=\s*https://[^@\s]+@github\.com/",
        cfg_text, _re.MULTILINE,
    ):
        return InstallStatus(
            bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="active",
        )

    # github.com is mentioned but no token-bearing URL — treat as missing
    # so the Skills card prompts the operator to run the backup onboard.
    return InstallStatus(
        bot_id=bot_id, skill_id=GITHUB_SKILL_ID, status="missing",
    )


def resolve_status(skill: UpstreamPluginSkill, bot_id: str) -> InstallStatus:
    """Inspect openclaw.json for *bot_id* and report install state for *skill*."""
    from ..config import bot_home

    # GitHub doesn't map to a plugins.entries.* row — see _resolve_github_status.
    if skill.id == GITHUB_SKILL_ID:
        return _resolve_github_status(bot_id)

    try:
        home = bot_home(bot_id)
    except Exception as exc:
        return InstallStatus(
            bot_id=bot_id, skill_id=skill.id, status="unknown",
            error=f"bot_home_failed: {exc}",
        )

    oc, err = _inv._read_oc_json(home)
    if oc is None:
        return InstallStatus(
            bot_id=bot_id, skill_id=skill.id, status="unknown",
            error=err or "no_openclaw_json",
        )

    plugins = (oc.get("plugins") or {}).get("entries") or {}
    plugin_cfg = plugins.get(skill.plugin_name)

    if plugin_cfg is None:
        return InstallStatus(bot_id=bot_id, skill_id=skill.id, status="missing")

    if plugin_cfg.get("enabled") is False:
        return InstallStatus(
            bot_id=bot_id, skill_id=skill.id, status="plugin_disabled",
        )

    if skill.auth_profile_key:
        profiles = (oc.get("auth") or {}).get("profiles") or {}
        # Profile keys are of shape "<provider>:<bot_id>" — match by prefix.
        prefix = f"{skill.auth_profile_key}:"
        has_profile = any(k.startswith(prefix) for k in profiles)
        if not has_profile:
            return InstallStatus(
                bot_id=bot_id, skill_id=skill.id, status="needs_auth",
            )

    return InstallStatus(bot_id=bot_id, skill_id=skill.id, status="active")


# ── Install plan ──────────────────────────────────────────────────────────────

@dataclass
class InstallStep:
    """One step for the modal to render. Upstream-plugin skills only ever
    produce a single informational step pointing to the deploy/OAuth path
    that actually does the work."""

    id: str
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


def build_install_plan(
    skill: UpstreamPluginSkill,
    status: InstallStatus,
) -> list[InstallStep]:
    """Return the remaining steps. Active → empty; otherwise a single info step."""
    if status.status == "active":
        return []
    if status.status == "unknown":
        return []

    # GitHub: hand off to the same Backup → Cloud guided wizard the operator
    # would use directly. The wizard does the PAT verify, repo create-or-reuse,
    # deploy-key registration, and per-bot .git/config wiring — the only thing
    # the legacy ``manual_setup`` hint asked the operator to do by hand.
    # The wizard self-closes on success, so no follow-up ``confirm`` step.
    if skill.id == GITHUB_SKILL_ID:
        return [
            InstallStep(
                id="open_github_backup_wizard",
                label=(
                    f"Open the GitHub backup wizard for "
                    f"{status.bot_id} — paste a PAT and we'll create the "
                    f"repo, register the deploy key, and wire up the remote."
                ),
                payload={"bot_id": status.bot_id},
                access_panel=dict(skill.access_panel),
            ),
        ]

    return [
        InstallStep(
            id="manual_setup",
            label=skill.install_hint,
            access_panel=dict(skill.access_panel),
        ),
        InstallStep(
            id="confirm",
            label=f"Confirm {skill.display_name} is working on this bot",
            endpoint=f"/api/skills/install/{skill.id}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


def get_skill(skill_id: str) -> UpstreamPluginSkill | None:
    """Lookup a skill by id. Returns None for unknown ids."""
    return SKILLS.get(skill_id)
