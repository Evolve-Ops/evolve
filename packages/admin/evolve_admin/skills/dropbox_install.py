"""evolve_admin.skills.dropbox_install — Dropbox folder filesystem skill installer.

Second MCP-server-backed paste-token-skill revival, following the Obsidian
pattern established in #1817. See:

  * ``project_obsidian_mcp_install_pattern`` (memory) — the 5-piece shape
  * ``docs/design/paste-token-skills-future-2026-05-30.md`` — the broader plan
  * ``docs/design/skills-install-roadmap-2026-05-30.md`` — current roadmap

Pre-2026-05-30 Dropbox was an entry in :mod:`upstream_plugin_skills` with
no real install path. Inventory §6 detection picked up
``~/.dropbox/info.json`` (the desktop client's own marker) and surfaced
the skill as "configured" on several bots — but no code ever used the Dropbox
folder for agent access. A 2026-05-29 plugins-page audit flagged this as
a false positive.

The rewire mirrors Obsidian closely because the runtime shape is the
same — a folder of regular files the bot should be able to read (and
optionally write). Differences from Obsidian:

  1. **Folder auto-detect.** The Dropbox desktop client writes
     ``~/.dropbox/info.json`` with the actual sync-folder path under
     ``personal.path`` (and/or ``business.path``). The install flow reads
     this to suggest the right path automatically — Obsidian had to ask.
  2. **Validation accepts any folder.** Obsidian validates against a
     blacklist of reserved system dirs. Dropbox uses the same blacklist
     but doesn't try to verify "this is really a Dropbox folder" —
     because users may legitimately want to scope the bot to a single
     project subfolder, not the whole sync root.
  3. **No "share with bot" Obsidian-app integration concern.** Dropbox
     handles sync at the file level; ACL grants compose cleanly with
     the Dropbox client's own permissions.

Everything else — the MCP install (catalog_id="filesystem",
extra_args=[folder_path]), the OS-ACL mode toggle (read vs read+write),
the mode-marker sidecar at ``~/.openclaw/skills/dropbox.json``, the
status resolver reading mcp.servers.dropbox — is structurally identical
to Obsidian. The shared helpers in this module are intentionally
copy-paste-of-Obsidian rather than DRY'd into a base class so each
skill stays self-contained while the pattern is young (per the
"keep skills independent" precedent set by the other install modules).
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

#: Canonical skill ID. Matches the existing "dropbox" key in inventory's
#: _PLUGIN_DISPLAY and the (now-removed) upstream_plugin_skills entry, so
#: bots with prior false-positive "configured" status migrate cleanly to
#: the new MCP-backed detection (§2 in inventory.py).
DROPBOX_SKILL_ID = "dropbox"

#: Skill kind — filesystem, same as Obsidian. Distinguishes from
#: plugin/oauth/mcp-non-filesystem skills in catalog metadata.
DROPBOX_SKILL_KIND = "filesystem"


# ── Plain-language access panel ──────────────────────────────────────────────
#
# Same two-mode shape as Obsidian's VAULT_ACCESS_PANEL — neutral panel
# exposes mode_choices for the UI radio; access_panel_for(mode) returns
# the mode-specific will/wont lists for the confirmation screen.

DROPBOX_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": DROPBOX_SKILL_ID,
    "skill_display_name": "Dropbox",
    "summary": (
        "Lets this bot work with files in your Dropbox. You choose a "
        "folder (your whole Dropbox or a specific project folder) and "
        "whether the bot can also create or edit files — read-only is "
        "the safe default; read-and-write lets the bot save notes, "
        "drafts, or generated documents when you ask it to."
    ),
    "will": [
        "Read files in the Dropbox folder you choose",
        "List subfolders and search for files by name",
        "Pull file contents into the bot's working context",
        "(read+write mode) Create new files, edit existing ones, and "
        "delete files you ask it to remove",
    ],
    "wont": [
        "Read or write files outside the folder you choose",
        "Connect to Dropbox cloud APIs or sign in to dropbox.com",
        "Share Dropbox contents with other bots or users on the pod",
        "Touch anything outside the folder — the bot's filesystem access "
        "is constrained to the path you grant",
    ],
    "where_credentials_live": (
        "No credentials are involved — your Dropbox is just a folder on "
        "this machine (synced by the Dropbox desktop client). Access is "
        "granted via a filesystem permission on the folder itself; "
        "remove the permission (or uninstall this skill) to revoke."
    ),
    "kind": DROPBOX_SKILL_KIND,
    # UI radio: read selected by default.
    "mode_choices": [
        {
            "value": "read",
            "label": "Read-only (recommended)",
            "description": (
                "Bot can read and search files in the folder but not "
                "modify them. Safe default — pick this unless you "
                "specifically want the bot to save files."
            ),
        },
        {
            "value": "read_write",
            "label": "Read and write",
            "description": (
                "Bot can read, create, edit, and delete files inside the "
                "folder. Useful for saving generated content (drafts, "
                "summaries, exported data) — be aware that bot-written "
                "files appear in Dropbox just like files you saved "
                "yourself, and will sync to your other devices."
            ),
        },
    ],
}


def access_panel_for(mode: str) -> dict[str, Any]:
    """Return the access panel specialised for ``mode``.

    Same shape as Obsidian's ``access_panel_for``. The neutral panel
    (unknown mode) keeps mode_choices so the UI radio still renders.
    """
    panel = dict(DROPBOX_ACCESS_PANEL)
    if mode == "read":
        panel["will"] = [
            "Read files in the Dropbox folder you choose",
            "List subfolders and search for files by name",
            "Pull file contents into the bot's working context",
        ]
        panel["wont"] = [
            "Create, edit, or delete any files in the folder",
            "Read or write files outside the folder you choose",
            "Connect to Dropbox cloud APIs or sign in to dropbox.com",
            "Share Dropbox contents with other bots or users on the pod",
        ]
        panel["mode"] = "read"
        return panel
    if mode == "read_write":
        panel["will"] = [
            "Read files in the Dropbox folder you choose",
            "List subfolders and search for files by name",
            "Create new files, edit existing ones, and save generated "
            "content (drafts, summaries) when you ask",
            "Delete files you specifically ask it to remove",
        ]
        panel["wont"] = [
            "Read or write files outside the folder you choose",
            "Connect to Dropbox cloud APIs or sign in to dropbox.com",
            "Share Dropbox contents with other bots or users on the pod",
            "Modify files without an explicit ask in the conversation",
        ]
        panel["mode"] = "read_write"
        return panel
    return panel


# ── Folder auto-detect (Dropbox-specific UX over Obsidian) ────────────────────
#
# The Dropbox desktop client writes ~/.dropbox/info.json under the
# user's home with the actual sync path. Reading it lets the install
# pre-fill the folder field with the path the user most likely wants —
# Obsidian had to ask "where is your vault?" because there's no
# equivalent canonical marker.
#
# info.json shape (Dropbox documents this):
#   {
#     "personal": {"path": "/Users/<user>/Dropbox", "host": ..., ...},
#     "business": {"path": "/Users/<user>/Dropbox (Work)", ...}
#   }
# Either or both keys may be present depending on which account types the
# user has connected.

DROPBOX_INFO_JSON_PATH = ".dropbox/info.json"


def find_dropbox_folder(home: Path) -> str | None:
    """Return the auto-detected Dropbox folder path for *home*, or None.

    Prefers ``personal.path`` over ``business.path`` (most users have
    personal; business is the niche case). Returns the FIRST valid path
    we find. Caller is responsible for further validation —
    :func:`validate_dropbox_path` enforces the safety blacklist.
    """
    info_path = home / DROPBOX_INFO_JSON_PATH
    text: str | None = None
    try:
        if info_path.exists():
            text = info_path.read_text(encoding="utf-8")
    except (PermissionError, OSError):
        pass
    if text is None:
        # Fallback: sudo /bin/cat — info.json may have restrictive perms
        # on macOS depending on the user's setup.
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(info_path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                text = r.stdout
        except Exception:
            pass
    if not text:
        return None
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(info, dict):
        return None
    for account_key in ("personal", "business"):
        acct = info.get(account_key)
        if isinstance(acct, dict):
            path = acct.get("path")
            if isinstance(path, str) and path.strip():
                return path
    return None


# ── Path validation ───────────────────────────────────────────────────────────
#
# Same reserved-location blacklist as obsidian_install.validate_vault_path,
# because the failure mode is identical — granting a filesystem MCP
# access to a system directory gives the bot read/write into places it
# absolutely should not have. We don't try to verify "this is really a
# Dropbox folder" because users may legitimately scope the bot to a
# specific project subfolder rather than the whole sync root.

# Per-segment prefixes — mirrors obsidian_install._VAULT_RESERVED_PREFIXES.
# /private is intentionally NOT a blanket prefix because /private/var/folders
# is the macOS NSTemporaryDirectory location (used by pytest, etc.) and
# must remain valid; we list the specific /private/{etc,tmp,var/log,...}
# subpaths separately. See obsidian_install for the symlink-bypass rationale.
_RESERVED_PATH_PREFIXES: tuple[str, ...] = (
    "/etc", "/var/log", "/var/root", "/var/spool", "/var/db",
    "/usr", "/bin", "/sbin", "/opt",
    "/System", "/Library", "/cores", "/tmp", "/dev",
    # macOS: /etc, /tmp, /var are symlinks → /private/{etc,tmp,var}.
    "/private/etc", "/private/tmp",
    "/private/var/log", "/private/var/db", "/private/var/root", "/private/var/spool",
)

_RESERVED_PATH_SUFFIXES_UNDER_USER: tuple[str, ...] = (
    "/.ssh", "/.aws", "/.gnupg", "/.openclaw", "/.dropbox",
    "/Library", "/.config",
)

_TOO_BROAD_PATHS: tuple[str, ...] = ("/", "/Users", "/Volumes")


def validate_dropbox_path(path_str: str) -> tuple[bool, str | None]:
    """Validate a Dropbox folder path against the reserved-location
    blacklist + basic existence/readability. Returns ``(ok, reason)``
    where reason is one of the documented error strings.

    Mirrors :func:`obsidian_install.validate_vault_path` so the Skills
    UI can render the same shape of error message regardless of which
    filesystem skill is being installed.
    """
    if not path_str or not path_str.strip():
        return False, "dropbox_path_empty"
    p = path_str.strip()

    if p in _TOO_BROAD_PATHS:
        return False, (
            "dropbox_path_too_broad: That folder is too broad — pick a "
            "specific Dropbox folder (your whole Dropbox or a project "
            "subfolder), not the whole filesystem root."
        )
    for prefix in _RESERVED_PATH_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return False, (
                "dropbox_path_reserved_location: That folder is reserved "
                "for system files — pick a folder under Documents, "
                "Desktop, or your Dropbox sync folder."
            )
    for suffix in _RESERVED_PATH_SUFFIXES_UNDER_USER:
        if p.endswith(suffix) or (suffix + "/") in p:
            return False, (
                f"dropbox_path_reserved_location: That folder ({suffix}) "
                "is reserved for credentials / system config — pick a "
                "different folder."
            )

    path = Path(p)
    if not path.exists():
        return False, (
            "dropbox_folder_not_found: That folder doesn't exist on this "
            "machine. Make sure the Dropbox desktop client has finished "
            "syncing and the path is correct."
        )
    if not path.is_dir():
        return False, "dropbox_path_not_a_directory"

    return True, None


# ── Install status + plan ─────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Dropbox skill install flow.

    Status values (small state machine — same as Obsidian's):

      * ``no_folder_configured`` — no mcp.servers.dropbox entry; UI prompts
        for a folder.
      * ``active`` — mcp.servers.dropbox present; bot's gateway loads the
        filesystem MCP scoped to the folder.
      * ``unknown`` — openclaw.json could not be read.
    """

    bot_id: str
    status: str  # no_folder_configured | active | unknown
    folder_path: str | None = None
    mode: str | None = None  # read | read_write | None (legacy install)
    suggested_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": DROPBOX_SKILL_ID,
            "kind": DROPBOX_SKILL_KIND,
            "status": self.status,
            "folder_path": self.folder_path,
            "mode": self.mode,
            "suggested_path": self.suggested_path,
            "error": self.error,
        }


@dataclass
class InstallStep:
    """One step the UI must drive to complete the Dropbox install."""

    id: str  # set_folder_path | confirm
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
    """Build the ordered steps remaining for *status*.

    Active or unknown → empty plan. Otherwise the set_folder_path step
    (with the auto-detected suggested_path in payload) + confirm step.
    """
    if status.status in ("active", "unknown"):
        return []

    return [
        InstallStep(
            id="set_folder_path",
            label="Choose your Dropbox folder and access mode",
            endpoint=f"/api/skills/install/{DROPBOX_SKILL_ID}/set-folder-path",
            payload={
                "bot_id": status.bot_id,
                "suggested_path": status.suggested_path or "",
            },
            access_panel=dict(DROPBOX_ACCESS_PANEL),
        ),
        InstallStep(
            id="confirm",
            label="Confirm the bot can read your Dropbox folder",
            endpoint=f"/api/skills/install/{DROPBOX_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        ),
    ]


# ── ACL grants (OS-permission-layer mode toggle) ─────────────────────────────
#
# Copy-paste of obsidian_install.grant_vault_acl / revoke_vault_acl
# with module-level constants renamed. Same chmod +a / chmod -a# idiom,
# same inheritance flags, same idempotency guarantee. See the original
# helpers for the design rationale (file_inherit + directory_inherit
# propagate to existing and future files; missing ACEs are treated as
# success on revoke so the same function can clean up after either mode).
#
# The mode toggle is enforced HERE: in read mode the bot user has no
# write ACE, so the filesystem MCP's write_file / create_directory
# return EACCES and the server forwards the error cleanly. No OC
# tool-denylist needed (OC doesn't currently support per-tool denial —
# see docs/design/paste-token-skills-future-2026-05-30.md).

DROPBOX_READ_ACE_PERMS = (
    "list,search,readattr,readextattr,readsecurity,"
    "file_inherit,directory_inherit"
)

DROPBOX_READ_WRITE_ACE_PERMS = (
    "list,search,add_file,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,"
    "file_inherit,directory_inherit"
)


def _ace_for(mode: str) -> str:
    if mode == "read":
        return DROPBOX_READ_ACE_PERMS
    if mode == "read_write":
        return DROPBOX_READ_WRITE_ACE_PERMS
    raise ValueError(f"unknown dropbox mode {mode!r} — expected 'read' or 'read_write'")


def grant_dropbox_acl(
    folder_path: str,
    bot_user: str,
    mode: str,
    *,
    runner: Any = subprocess.run,
) -> tuple[bool, str | None]:
    """Grant *bot_user* ACL access on *folder_path* with the requested *mode*.

    Mode is ``"read"`` or ``"read_write"``. Idempotent — chmod +a treats
    a duplicate ACE as a no-op. Applies to the dir AND existing children
    via the recursive pass; new files inherit via the flags.
    """
    try:
        ace = f"{bot_user} allow {_ace_for(mode)}"
    except ValueError as exc:
        return False, str(exc)

    if not Path(folder_path).exists():
        return False, f"folder_path does not exist: {folder_path}"

    r = runner(
        ["sudo", "/bin/chmod", "+a", ace, folder_path],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (
            f"chmod +a on folder root failed: "
            f"{(r.stderr or r.stdout or '').strip() or 'unknown'}"
        )

    r = runner(
        ["sudo", "/bin/chmod", "-R", "+a", ace, folder_path],
        capture_output=True, text=True, timeout=120,  # Dropbox folders can be large
    )
    if r.returncode != 0:
        return False, (
            f"chmod -R +a on folder tree failed: "
            f"{(r.stderr or r.stdout or '').strip() or 'unknown'}"
        )

    return True, None


def revoke_dropbox_acl(
    folder_path: str,
    bot_user: str,
    *,
    runner: Any = subprocess.run,
) -> tuple[bool, str | None]:
    """Remove *bot_user*'s ACEs from *folder_path* (both read and read_write).

    Best-effort — runs ``chmod -a`` against each known mode's ACE so a
    later re-install with a different mode starts clean. ACL-not-found
    is treated as success (idempotent).
    """
    if not Path(folder_path).exists():
        return True, None

    errors: list[str] = []
    for mode in ("read", "read_write"):
        ace = f"{bot_user} allow {_ace_for(mode)}"
        for argv in (
            ["sudo", "/bin/chmod", "-a", ace, folder_path],
            ["sudo", "/bin/chmod", "-R", "-a", ace, folder_path],
        ):
            r = runner(argv, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip().lower()
                if "not found" in msg or "no such ace" in msg:
                    continue
                errors.append(
                    f"{' '.join(argv[:3])} on folder failed: {msg or 'unknown'}"
                )

    if errors:
        return False, "; ".join(errors[:3])
    return True, None


# ── Mode marker sidecar ───────────────────────────────────────────────────────
#
# Records {folder_path, mode} so the status resolver can answer "what
# mode is this install in?" for the UI. The MCP applier writes only
# command + args to openclaw.json (no place to store the mode), so this
# sidecar is the source of truth. **Must NOT trigger inventory.py §3
# detection** — that's the dead-end pattern. Inventory looks at
# mcp.servers.dropbox (§2); this file is consulted only by
# resolve_status_mcp via an injected callable.

MODE_MARKER_PATH = ".openclaw/skills/dropbox.json"


def mode_marker_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / MODE_MARKER_PATH


def read_mode_marker(bot_id: str) -> dict | None:
    """Read the mode marker for *bot_id*. Returns None if missing/unreadable."""
    p = mode_marker_path(bot_id)
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
    return None


def write_mode_marker(
    bot_id: str,
    folder_path: str,
    mode: str,
) -> tuple[bool, str | None]:
    """Persist {folder_path, mode} for *bot_id* via /tmp staging + sudo /bin/cp."""
    from ..config import bot_home as _bot_home, get_bot_user, load_network

    if mode not in ("read", "read_write"):
        return False, f"unknown mode {mode!r}"

    network = load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / MODE_MARKER_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    payload = {
        "folder_path": folder_path,
        "mode": mode,
        "skill_id": DROPBOX_SKILL_ID,
    }

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-dbx-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)

        r = subprocess.run(
            ["sudo", "/bin/mkdir", "-p", skills_dir],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"mkdir failed: {(r.stderr or '').strip() or 'unknown'}"

        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", skills_dir],
            capture_output=True, text=True, timeout=10,
        )

        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, dest],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"cp failed: {(r.stderr or '').strip() or 'unknown'}"

        subprocess.run(
            ["sudo", "/usr/sbin/chown", f"{user}:staff", dest],
            capture_output=True, text=True, timeout=10,
        )
        return True, None
    except Exception as exc:
        return False, f"write_mode_marker_error: {exc.__class__.__name__}: {exc}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def delete_mode_marker(bot_id: str) -> bool:
    """Best-effort delete the mode marker. Returns True if cleared."""
    p = mode_marker_path(bot_id)
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


# ── MCP-aware status resolver ─────────────────────────────────────────────────


def resolve_status_mcp(
    bot_id: str,
    *,
    read_oc_config: Callable,  # (bot_id) -> (dict | None, err)
    read_marker: Callable | None = None,  # (bot_id) -> dict | None
) -> InstallStatus:
    """Resolve install status from the bot's openclaw.json + mode marker.

    Active when ``mcp.servers.dropbox`` is present in the bot's
    openclaw.json (the §2 OC loader signal — see
    audit-plugins-page-2026-05-29.md). Mode comes from the marker;
    absent marker → mode=None (legacy install / pre-marker bot).

    Drift between marker.folder_path and openclaw.json args[0] surfaces
    via ``error`` so the UI can warn — the openclaw.json value is the
    truth (that's what the gateway actually loads).
    """
    _read_marker = read_marker or read_mode_marker

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
    entry = servers.get("dropbox")
    if not isinstance(entry, dict):
        return InstallStatus(bot_id=bot_id, status="no_folder_configured")

    args = entry.get("args") or []
    folder_path = args[0] if args else None

    marker = None
    try:
        marker = _read_marker(bot_id)
    except Exception:
        marker = None
    mode = (marker or {}).get("mode")
    marker_folder = (marker or {}).get("folder_path")

    error: str | None = None
    if marker_folder and folder_path and marker_folder != folder_path:
        error = (
            f"mode_marker_drift: marker says {marker_folder!r}, "
            f"openclaw.json says {folder_path!r}"
        )

    return InstallStatus(
        bot_id=bot_id,
        status="active",
        folder_path=folder_path,
        mode=mode,
        error=error,
    )


# ── Skill registry entry ──────────────────────────────────────────────────────


SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": DROPBOX_SKILL_ID,
    "kind": DROPBOX_SKILL_KIND,
    "display_name": DROPBOX_ACCESS_PANEL["skill_display_name"],
    "summary": DROPBOX_ACCESS_PANEL["summary"],
    "access_panel": dict(DROPBOX_ACCESS_PANEL),
    # Surfaced by /api/skills/catalog/<id> for the UI install modal.
    "config_keys": ["folder_path", "mode"],
}
