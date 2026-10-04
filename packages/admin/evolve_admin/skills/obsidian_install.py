"""evolve_admin.skills.obsidian_install — Obsidian vault filesystem skill installer.

.. note::
   **Rewired 2026-05-30 as the reference impl for paste-token-skills-future.**
   The original install was a dead-end (wrote ``skills/obsidian_vault.json``
   with no runtime consumer). It now installs the vetted
   ``@modelcontextprotocol/server-filesystem`` MCP server scoped to the
   user's chosen vault path, with a read / read+write toggle enforced at
   the OS file-permission layer via macOS ACLs (the filesystem MCP
   surfaces ``write_file`` / ``create_directory`` either way; the kernel
   denies them when the bot user only has read perms on the vault, which
   the server forwards to MCP clients as a clean error). See
   ``internal/design/paste-token-skills-future-2026-05-30.md`` for the wider
   pattern and the four other withdrawn skills that should follow this
   shape.

Obsidian is a filesystem-based knowledge tool: vaults are just directories of
Markdown files. No OAuth, no API key, no external service. The "skill" is
access to a directory on disk that the bot can read (and, if opted in,
append to for the daily note).

This is the first ``kind=filesystem`` skill in Evolve's catalog. Future
filesystem skills (a generic markdown-folder skill, a local wiki, a note-taking
app) should follow this pattern:

  1. A canonical skill ID.
  2. A ``VAULT_ACCESS_PANEL`` describing what the bot will/won't do with the
     path — written for the Plex test (no jargon; concrete user-recognizable
     actions).
  3. An ``InstallStatus`` + ``build_install_plan`` pair: status captures where
     the install is in a small state machine; the plan tells the UI what steps
     remain.
  4. A ``resolve_status`` that depends only on injected callables so it is
     unit-testable without touching the filesystem.

Design decisions:

  1. **Read-only by default; write only for daily-note append.** The default
     install grants read access to the vault directory only. ``append_to_daily_note``
     requires an explicit opt-in (``write_daily_note: true`` in the config).
     This maps to distinct exec-approval entries: ``vault_read`` always; ``vault_write``
     only when the opt-in is set.

  2. **Exec-approvals scoped to vault directory.** The vault path is the exec-approval
     boundary. Nothing in this skill reads or writes outside ``vault_path/``. The
     install validator enforces this: ``vault_path`` must be an absolute path to an
     existing directory.

  3. **Common default paths.** The installer checks common Obsidian vault locations
     (``~/Documents/Obsidian``, ``~/Obsidian``, ``~/Desktop/Obsidian``) and suggests
     the first one found. User can override.

  4. **No plugin, no provider.** Unlike GOG, Obsidian needs no OpenClaw plugin
     entry. Access is purely filesystem. The ``kind`` field is ``"filesystem"`` to
     distinguish from plugin-backed or MCP-backed skills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Skill identifier ──────────────────────────────────────────────────────────

#: Canonical skill ID for the filesystem-based Obsidian skill.
#: Uses "obsidian_vault" (not "obsidian") to avoid collision with any future
#: OpenClaw plugin also named "obsidian" in _PLUGIN_DISPLAY. The Python constant
#: name stays OBSIDIAN_SKILL_ID for readability; only the VALUE changes.
OBSIDIAN_SKILL_ID = "obsidian_vault"

#: Skill kind — "filesystem" distinguishes this from plugin/oauth/mcp skills.
OBSIDIAN_SKILL_KIND = "filesystem"

#: Common vault locations, in priority order.
#: The installer tries these if the user hasn't configured a vault path yet.
OBSIDIAN_DEFAULT_VAULT_CANDIDATES: list[Path] = [
    Path("~/Documents/Obsidian").expanduser(),
    Path("~/Documents/Obsidian Vault").expanduser(),  # Obsidian's actual macOS default
    Path("~/Obsidian").expanduser(),
    Path("~/Desktop/Obsidian").expanduser(),
]


# ── Plain-language access panel ───────────────────────────────────────────────
#
# Two modes — "read" and "read_write" — produce two distinct will/wont
# promises. The neutral access panel below is used at the FIRST install
# step (before the user has picked a mode) and is honest that the mode
# choice exists; the per-mode panels at the bottom of this section are what
# the UI surfaces on the confirmation screen after the mode is chosen.

VAULT_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": OBSIDIAN_SKILL_ID,
    "skill_display_name": "Obsidian Vault",
    "summary": (
        "Lets this bot work with notes in your Obsidian vault. "
        "You choose a vault folder and whether the bot can also write notes — "
        "read-only is the safe default; read-and-write lets the bot append "
        "to existing notes or create new ones when you ask it to."
    ),
    "will": [
        "Read Markdown files in the vault folder you choose",
        "Search notes by keyword, tag, or date",
        "Surface recent notes in your morning briefing (if you ask)",
        # write-side capabilities only appear when the user picks read_write
        # mode — the per-mode panel below carries the precise copy.
        "(read+write mode) Create new notes and append to existing ones",
    ],
    "wont": [
        "Read or write files outside the vault folder you choose",
        "Upload your notes to Obsidian Sync, Obsidian Publish, or any cloud API",
        "Share vault contents with other bots or users",
        # In read mode the OS denies writes; in read+write mode the bot can
        # still touch files but only inside the vault you picked.
        "Touch anything outside the vault — the bot's filesystem access is "
        "constrained to the path you grant",
    ],
    "where_credentials_live": (
        "No credentials are involved — your vault is just a folder on this "
        "machine. Access is granted via a filesystem permission on the vault "
        "directory itself; remove the permission (or uninstall this skill) "
        "to revoke."
    ),
    "kind": OBSIDIAN_SKILL_KIND,
    # The UI radio uses this to populate the mode toggle. Order matters —
    # read is the default-selected option.
    "mode_choices": [
        {
            "value": "read",
            "label": "Read-only (recommended)",
            "description": (
                "Bot can read and search notes but not modify them. "
                "Safe default — pick this unless you specifically want the "
                "bot to write notes."
            ),
        },
        {
            "value": "read_write",
            "label": "Read and write",
            "description": (
                "Bot can read, create, edit, and delete notes inside the vault. "
                "Useful for daily-note appends and bot-authored summaries; "
                "be aware that bot-written notes are visible in your vault "
                "just like notes you wrote yourself."
            ),
        },
    ],
}


def access_panel_for(mode: str) -> dict[str, Any]:
    """Return the access panel specialised for ``mode``.

    Used on the confirmation screen after the user has picked read vs
    read_write so the will/wont lists reflect the actual scope of access
    being granted. Falls back to the neutral panel (with both-mode copy)
    when ``mode`` is unknown.
    """
    panel = dict(VAULT_ACCESS_PANEL)
    if mode == "read":
        panel["will"] = [
            "Read Markdown files in the vault folder you choose",
            "Search notes by keyword, tag, or date",
            "Surface recent notes in your morning briefing (if you ask)",
        ]
        panel["wont"] = [
            "Create, edit, or delete any notes in the vault",
            "Read or write files outside the vault folder you choose",
            "Upload your notes to Obsidian Sync, Obsidian Publish, or any cloud API",
            "Share vault contents with other bots or users",
        ]
        panel["mode"] = "read"
        return panel
    if mode == "read_write":
        panel["will"] = [
            "Read Markdown files in the vault folder you choose",
            "Search notes by keyword, tag, or date",
            "Create new notes, edit existing ones, and append to daily notes",
            "Delete notes you ask it to remove",
        ]
        panel["wont"] = [
            "Read or write files outside the vault folder you choose",
            "Upload your notes to Obsidian Sync, Obsidian Publish, or any cloud API",
            "Share vault contents with other bots or users",
            "Modify notes without an explicit ask in the conversation",
        ]
        panel["mode"] = "read_write"
        return panel
    return panel


# ── Install status ────────────────────────────────────────────────────────────


@dataclass
class InstallStatus:
    """Snapshot of where a bot is in the Obsidian skill install flow.

    Status values (small state machine):

    * ``no_vault_configured`` — no vault path is set; UI must prompt for path.
    * ``vault_not_found`` — a vault path is configured but doesn't exist on disk.
    * ``vault_not_readable`` — the vault directory exists but isn't readable
      by the bot user.
    * ``active`` — vault exists, is readable, bot can use the skill.
    * ``unknown`` — pre-flight read failed; ``error`` has the detail.
    """

    bot_id: str
    status: str  # no_vault_configured | vault_not_found | vault_not_readable | active | unknown
    vault_path: str | None = None
    write_daily_note_enabled: bool = False
    note_count: int | None = None
    suggested_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "skill_id": OBSIDIAN_SKILL_ID,
            "kind": OBSIDIAN_SKILL_KIND,
            "status": self.status,
            "vault_path": self.vault_path,
            "write_daily_note_enabled": self.write_daily_note_enabled,
            "note_count": self.note_count,
            "suggested_path": self.suggested_path,
            "error": self.error,
        }


# ── Install plan ──────────────────────────────────────────────────────────────


@dataclass
class InstallStep:
    """One step the UI must drive to complete the Obsidian install.

    Steps are in order. ``id`` is what the UI dispatches on; ``label`` is
    the human-readable progress string.
    """

    id: str  # one of: set_vault_path | confirm
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

    * ``no_vault_configured`` or ``vault_not_found`` → set_vault_path step (with
      access panel), then confirm.
    * ``vault_not_readable`` → same: the user may need to choose a different path
      or fix permissions.
    * ``active`` → empty plan (nothing to do; UI shows success).
    * ``unknown`` → empty plan; UI surfaces the error.
    """
    if status.status == "active":
        return []

    if status.status == "unknown":
        return []

    plan: list[InstallStep] = []

    plan.append(
        InstallStep(
            id="set_vault_path",
            label="Choose your Obsidian vault folder",
            endpoint=f"/api/skills/install/{OBSIDIAN_SKILL_ID}/set-vault-path",
            payload={
                "bot_id": status.bot_id,
                "suggested_path": status.suggested_path or "",
            },
            access_panel=dict(VAULT_ACCESS_PANEL),
        )
    )

    plan.append(
        InstallStep(
            id="confirm",
            label="Confirm the bot can read your vault",
            endpoint=f"/api/skills/install/{OBSIDIAN_SKILL_ID}/status",
            payload={"bot_id": status.bot_id},
        )
    )

    return plan


# ── Pure resolver — works off injected callables for testability ──────────────


def _find_default_vault() -> Path | None:
    """Return the first candidate vault path that exists, or None."""
    for candidate in OBSIDIAN_DEFAULT_VAULT_CANDIDATES:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None



# ── Vault path security: reserved-location blacklist ─────────────────────────
#
# These path prefixes are unconditionally rejected regardless of whether the
# directory exists or is readable. A vault inside any of them would give the
# bot (and the write_daily_note feature) write access to system or sensitive
# user files — the ~/.ssh-as-vault attack being the canonical example.
#
# IMPORTANT: We check the path BOTH before and after resolve() to defeat
# symlink-based bypasses. The pre-resolve check catches canonical spellings
# (e.g. /etc, /tmp); the post-resolve check catches macOS symlinks
# (/tmp → /private/tmp, /var → /private/var, /etc → /private/etc).
#
# We use per-segment prefixes ("/private/etc", "/private/tmp", "/private/var")
# rather than all of "/private" because /private/var/folders is the macOS
# per-user temp-file area (used by pytest, NSTemporaryDirectory, etc.) and
# must remain valid for test environments.
#
# macOS-specific paths (/System, /Applications, /Library) are included
# because this skill targets macOS-hosted bots on the mini.
_VAULT_RESERVED_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/Library",
    "/System",
    "/Applications",
    "/var",
    "/tmp",
    "/dev",
    "/usr",
    "/bin",
    "/sbin",
    # macOS: /etc, /tmp, /var are symlinks → /private/{etc,tmp,var}.
    # resolve() follows them; block the resolved forms explicitly so a user
    # cannot bypass the check by spelling /etc as a symlink target.
    # /private/var/folders is NOT blocked here (it's the macOS tmpdir).
    "/private/etc",
    "/private/tmp",
    "/private/var/log",
    "/private/var/db",
    "/private/var/root",
    "/private/var/spool",
)

# Per-user sensitive directories.
# These are checked via expanduser() on the current user, then compared against
# the resolved vault path. This catches ~/.ssh even if the user supplies the
# literal expanded path (e.g. /Users/team_bot_a/.ssh).
_VAULT_RESERVED_USER_DIRS: tuple[str, ...] = (
    "/.ssh",
    "/.gnupg",
    "/.aws",
    "/.config",
    "/Library",
)

_RESERVED_LOCATION_ERROR = (
    "vault_path_reserved_location: That folder is reserved for system "
    "files — pick a folder under Documents, Desktop, or another "
    "personal-files area."
)


def _is_reserved(vault_str: str) -> bool:
    """Return True if *vault_str* (an absolute, resolved path string) is
    inside any of the reserved system-level prefixes."""
    for prefix in _VAULT_RESERVED_PREFIXES:
        if vault_str == prefix or vault_str.startswith(prefix + "/"):
            return True
    return False


def _is_reserved_user_dir(vault_str: str) -> bool:
    """Return True if *vault_str* is inside a per-user sensitive directory
    (e.g. ~/.ssh, ~/Library) for the currently-running user."""
    home = str(Path.home())
    for suffix in _VAULT_RESERVED_USER_DIRS:
        sensitive = home + suffix
        if vault_str == sensitive or vault_str.startswith(sensitive + "/"):
            return True
    return False


def validate_vault_path(vault_path_str: str) -> tuple[bool, str | None]:
    """Validate a vault path string.

    Returns (ok, error_reason). error_reason is None when ok is True.

    Validation rules:
    - Must be an absolute path.
    - Must not fall inside a reserved system or sensitive-user location
      (hard reject — see _VAULT_RESERVED_PREFIXES and
      _VAULT_RESERVED_USER_DIRS). Checked both pre- and post-resolve()
      to defeat symlink bypasses.
    - Must resolve to an existing directory.
    - Must not be a filesystem root (``/``) — too broad.
    - Must be readable (we test via iterdir attempt).

    These rules enforce the exec-approval scope: the vault is a bounded,
    user-chosen directory, not the entire filesystem.
    """
    if not vault_path_str or not vault_path_str.strip():
        return False, "vault_path_empty"

    raw_stripped = vault_path_str.strip()

    try:
        expanded = Path(raw_stripped).expanduser()
        vault_path = expanded.resolve()
    except (TypeError, ValueError) as exc:
        return False, f"vault_path_invalid: {exc}"

    if not vault_path.is_absolute():
        return False, "vault_path_must_be_absolute"

    vault_str = str(vault_path)

    # ── Reserved-location check (hard reject) ─────────────────────────────────
    # Check both the expanded-but-pre-resolve form (catches canonical names like
    # /etc before the symlink is followed) and the fully-resolved form (catches
    # /tmp → /private/tmp etc.).
    pre_resolve_str = str(expanded)
    if _is_reserved(pre_resolve_str) or _is_reserved(vault_str):
        return False, _RESERVED_LOCATION_ERROR

    if _is_reserved_user_dir(pre_resolve_str) or _is_reserved_user_dir(vault_str):
        return False, _RESERVED_LOCATION_ERROR

    if vault_str in ("/", "/Users", "/home"):
        return False, "vault_path_too_broad"

    if not vault_path.exists():
        return False, "vault_not_found"

    if not vault_path.is_dir():
        return False, "vault_not_a_directory"

    # Check readability by attempting a directory listing (non-recursive).
    try:
        next(vault_path.iterdir(), None)
    except PermissionError:
        return False, "vault_not_readable"
    except OSError as exc:
        return False, f"vault_read_error: {exc}"

    return True, None


def resolve_status(
    bot_id: str,
    *,
    read_vault_config,  # callable(bot_id) -> dict | None ; returns {"vault_path": ..., "write_daily_note": ...}
    check_path_readable,  # callable(path_str) -> tuple[bool, str|None]
    find_suggested_vault,  # callable() -> str | None ; suggests a default path
) -> InstallStatus:
    """Pure-Python status resolver.

    Reader callables abstract over the real config/filesystem so this function
    is unit-testable without real bots. The route layer wires the real
    implementations; tests pass in dict/lambda stubs.

    Never raises on bad reader output — any reader error is captured as
    ``status="unknown"`` with ``error`` populated.
    """
    # ── Read config ───────────────────────────────────────────────────────────
    try:
        config = read_vault_config(bot_id)
    except Exception as exc:  # pragma: no cover — defensive
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            error=f"config_read_failed: {exc.__class__.__name__}: {exc}",
        )

    vault_path_str: str | None = (config or {}).get("vault_path") or None
    write_daily_note = bool((config or {}).get("write_daily_note", False))

    # ── Suggest a default if no path is configured ────────────────────────────
    try:
        suggested = find_suggested_vault()
    except Exception:  # pragma: no cover — defensive
        suggested = None

    if not vault_path_str:
        return InstallStatus(
            bot_id=bot_id,
            status="no_vault_configured",
            write_daily_note_enabled=write_daily_note,
            suggested_path=suggested,
        )

    # ── Validate the configured path ──────────────────────────────────────────
    try:
        ok, reason = check_path_readable(vault_path_str)
    except Exception as exc:  # pragma: no cover — defensive
        return InstallStatus(
            bot_id=bot_id,
            status="unknown",
            vault_path=vault_path_str,
            write_daily_note_enabled=write_daily_note,
            error=f"path_check_failed: {exc.__class__.__name__}: {exc}",
        )

    if not ok:
        status_str = "vault_not_found" if reason == "vault_not_found" else "vault_not_readable"
        return InstallStatus(
            bot_id=bot_id,
            status=status_str,
            vault_path=vault_path_str,
            write_daily_note_enabled=write_daily_note,
            suggested_path=suggested,
            error=reason,
        )

    # ── Count notes (best-effort; non-fatal) ─────────────────────────────────
    note_count: int | None = None
    try:
        vault = Path(vault_path_str).expanduser().resolve()
        note_count = sum(1 for _ in vault.rglob("*.md"))
    except Exception:  # pragma: no cover — count is informational
        pass

    return InstallStatus(
        bot_id=bot_id,
        status="active",
        vault_path=vault_path_str,
        write_daily_note_enabled=write_daily_note,
        note_count=note_count,
    )


# ── Skill registry entry ──────────────────────────────────────────────────────

SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": OBSIDIAN_SKILL_ID,
    "kind": OBSIDIAN_SKILL_KIND,
    "display_name": VAULT_ACCESS_PANEL["skill_display_name"],
    "summary": VAULT_ACCESS_PANEL["summary"],
    "access_panel": dict(VAULT_ACCESS_PANEL),
    # No plugin_name, provider_id, or default_services — filesystem skill needs none.
    "config_keys": ["vault_path", "write_daily_note"],
}


# ── ACL grants for the MCP-server install path ────────────────────────────────
#
# The filesystem MCP server runs as the bot user (the OC gateway's uid).
# For the bot to read or write files in the user's vault (which is owned by
# the human user, not the bot user) the bot user needs ACL access on the
# vault directory. Mirrors set_evolve_read_acl in deploy.py — same chmod +a
# / chmod -a# idiom, same inheritance flags, same "idempotent on repeated
# call" property.
#
# The read vs read+write toggle is enforced HERE: the MCP server still
# advertises write_file / create_directory tools, but if the bot user only
# holds the read ACE, the kernel returns EACCES on any write attempt and
# the server forwards the error to the MCP client. No tool denylist needed
# at the OC layer (which doesn't currently support one anyway — see
# internal/design/paste-token-skills-future-2026-05-30.md).

import subprocess

#: ACE for read-only access. file_inherit + directory_inherit propagate the
#: grant to existing AND future files in the vault, so a new note created
#: by the user (after install) is automatically readable by the bot.
VAULT_READ_ACE_PERMS = (
    "list,search,readattr,readextattr,readsecurity,"
    "file_inherit,directory_inherit"
)

#: ACE for read+write access. Adds the standard write-side permissions
#: (write file content, create new files, create subdirectories, delete,
#: write extended attrs). Files the bot creates remain *owned* by the bot
#: user — if the human user can't edit them via Obsidian afterwards, an
#: explicit chown is required (see open-question #3 in the design doc).
VAULT_READ_WRITE_ACE_PERMS = (
    "list,search,add_file,add_subdirectory,"
    "readattr,writeattr,readextattr,writeextattr,"
    "readsecurity,delete,write,"
    "file_inherit,directory_inherit"
)


def _ace_for(mode: str) -> str:
    """Return the chmod +a permission spec for the given install mode."""
    if mode == "read":
        return VAULT_READ_ACE_PERMS
    if mode == "read_write":
        return VAULT_READ_WRITE_ACE_PERMS
    raise ValueError(f"unknown vault mode {mode!r} — expected 'read' or 'read_write'")


def grant_vault_acl(
    vault_path: str,
    bot_user: str,
    mode: str,
    *,
    runner: Any = subprocess.run,
) -> tuple[bool, str | None]:
    """Grant *bot_user* ACL access on *vault_path* with the requested *mode*.

    Mode is ``"read"`` or ``"read_write"``. Idempotent — chmod +a treats a
    duplicate ACE as a no-op. Applies to the dir AND existing files via the
    recursive pass, then relies on file_inherit + directory_inherit for new
    files. Pre-existing different-mode ACE for the same bot_user is NOT
    cleared by this function; callers that change a bot's mode should call
    :func:`revoke_vault_acl` first.

    Returns ``(True, None)`` on success, ``(False, error)`` on chmod failure
    (vault path missing, sudoers misconfigured, etc.). ``runner`` is the
    subprocess-run callable (injectable for tests).
    """
    try:
        ace = f"{bot_user} allow {_ace_for(mode)}"
    except ValueError as exc:
        return False, str(exc)

    if not Path(vault_path).exists():
        return False, f"vault_path does not exist: {vault_path}"

    # 1. Apply to the dir with inheritance flags
    r = runner(
        ["sudo", "/bin/chmod", "+a", ace, vault_path],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (
            f"chmod +a on vault root failed: "
            f"{(r.stderr or r.stdout or '').strip() or 'unknown'}"
        )

    # 2. Retroactively apply to existing children so files created BEFORE the
    # install are visible. New files inherit via the flags above.
    r = runner(
        ["sudo", "/bin/chmod", "-R", "+a", ace, vault_path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return False, (
            f"chmod -R +a on vault tree failed: "
            f"{(r.stderr or r.stdout or '').strip() or 'unknown'}"
        )

    return True, None


def revoke_vault_acl(
    vault_path: str,
    bot_user: str,
    *,
    runner: Any = subprocess.run,
) -> tuple[bool, str | None]:
    """Remove *bot_user*'s ACEs from *vault_path* (both read and read_write).

    Best-effort — runs ``chmod -a#`` against each known mode's ACE so a
    later re-install with a different mode starts clean. Used by the
    Obsidian skill's revoke path before :func:`grant_vault_acl` re-applies
    a different mode, and by the uninstall handler when the skill is removed
    from the bot entirely.

    Returns ``(True, None)`` if at least the dir-level removals succeeded
    (idempotent — missing ACEs are OK), ``(False, error)`` on hard failure.
    """
    if not Path(vault_path).exists():
        # Already gone — nothing to revoke. Treat as success so callers
        # don't have to special-case missing vault dirs.
        return True, None

    errors: list[str] = []
    for mode in ("read", "read_write"):
        ace = f"{bot_user} allow {_ace_for(mode)}"
        # chmod -a removes a matching ACE; exit code 1 + "ACL entry not
        # found" is the harmless case when this mode wasn't installed.
        for argv in (
            ["sudo", "/bin/chmod", "-a", ace, vault_path],
            ["sudo", "/bin/chmod", "-R", "-a", ace, vault_path],
        ):
            r = runner(argv, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip().lower()
                if "not found" in msg or "no such ace" in msg:
                    continue  # expected: that mode wasn't applied
                errors.append(
                    f"{' '.join(argv[:3])} on vault failed: {msg or 'unknown'}"
                )

    if errors:
        return False, "; ".join(errors[:3])  # first 3 to keep response short
    return True, None


# ── Mode marker (records what mode the bot was installed at) ─────────────────
#
# The MCP applier writes only ``command`` + ``args`` into
# mcp.servers.obsidian, so there's nowhere in openclaw.json to record
# whether the install is read or read_write. We persist the mode in a small
# sidecar file owned by the bot user. The status resolver reads it to
# answer "what mode am I in?" for the admin UI; absence means a pre-rewire
# or legacy install whose mode is unknown.
#
# This file's existence does NOT trigger inventory.py §3 detection (we
# removed obsidian_vault from _FILESYSTEM_SKILLS). It's a UI-state-only
# marker, not a "configured" signal.

import json as _json
import os as _os
import tempfile as _tempfile

MODE_MARKER_PATH = ".openclaw/skills/obsidian_vault.json"


def mode_marker_path(bot_id: str) -> Path:
    from ..config import bot_home as _bot_home
    return _bot_home(bot_id) / MODE_MARKER_PATH


def read_mode_marker(bot_id: str) -> dict | None:
    """Read the mode marker for *bot_id*. Returns None if missing or unreadable."""
    p = mode_marker_path(bot_id)
    try:
        if p.exists():
            return _json.loads(p.read_text(encoding="utf-8"))
    except (PermissionError, OSError, _json.JSONDecodeError):
        pass
    # Fallback: sudo /bin/cat (pre-ACL-setup bots).
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return _json.loads(r.stdout)
    except Exception:
        pass
    return None


def write_mode_marker(
    bot_id: str,
    vault_path: str,
    mode: str,
) -> tuple[bool, str | None]:
    """Persist the install mode for *bot_id* via /tmp staging + sudo /bin/cp.

    Records {vault_path, mode, installed_at} so the admin UI can answer
    "what mode is this install in?" without inspecting ACLs. Following the
    CLAUDE.md bot-files write pattern (the bot owns this file).
    """
    from ..config import bot_home as _bot_home, get_bot_user, load_network

    if mode not in ("read", "read_write"):
        return False, f"unknown mode {mode!r}"

    network = load_network()
    user = get_bot_user(bot_id, network)
    home = _bot_home(bot_id, network)
    dest = str(home / MODE_MARKER_PATH)
    skills_dir = str(home / ".openclaw" / "skills")

    payload = {
        "vault_path": vault_path,
        "mode": mode,
        "skill_id": OBSIDIAN_SKILL_ID,
    }

    fd, tmp = _tempfile.mkstemp(dir="/tmp", prefix=f"evolve-{bot_id}-obs-", suffix=".json")
    try:
        with _os.fdopen(fd, "w") as f:
            _json.dump(payload, f, indent=2)

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
            _os.unlink(tmp)
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
    read_oc_config,  # callable(bot_id) -> (dict | None, err)
    read_marker=None,  # callable(bot_id) -> dict | None; defaults to read_mode_marker
) -> InstallStatus:
    """Resolve install status from the bot's openclaw.json + mode marker.

    The skill is ``active`` when ``mcp.servers.obsidian`` exists in the
    bot's openclaw.json — that's the authoritative loader-side signal
    (per internal/audit-plugins-page-2026-05-29.md's §2 detection branch). The
    mode marker is supplementary: present → status reports the mode;
    missing → status reports mode=None (legacy install or just-rewired
    bot whose marker hasn't landed yet).

    Status values (subset of the original state machine):
      * ``active`` — mcp.servers.obsidian present; bot's gateway loads
        the filesystem MCP scoped to the vault.
      * ``missing`` — no mcp.servers.obsidian entry; the install hasn't
        run on this bot or was revoked.
      * ``unknown`` — openclaw.json could not be read.
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
    entry = servers.get("obsidian")
    if not isinstance(entry, dict):
        return InstallStatus(bot_id=bot_id, status="no_vault_configured")

    args = entry.get("args") or []
    vault_path = args[0] if args else None

    marker = None
    try:
        marker = _read_marker(bot_id)
    except Exception:
        marker = None
    mode = (marker or {}).get("mode")
    marker_vault = (marker or {}).get("vault_path")

    # Drift detection: marker vault path != installed args. Report drift
    # by surfacing the installed path (truth) but flagging the marker as
    # stale via write_daily_note_enabled=False (UI key was preserved for
    # back-compat; downstream UI uses status + vault_path).
    if marker_vault and vault_path and marker_vault != vault_path:
        error = (
            f"mode_marker_drift: marker says {marker_vault!r}, "
            f"openclaw.json says {vault_path!r}"
        )
    else:
        error = None

    return InstallStatus(
        bot_id=bot_id,
        status="active",
        vault_path=vault_path,
        # write_daily_note_enabled doubles as the read+write boolean for
        # backward compat with the existing dict-to-dict callers.
        write_daily_note_enabled=(mode == "read_write"),
        error=error,
    )
