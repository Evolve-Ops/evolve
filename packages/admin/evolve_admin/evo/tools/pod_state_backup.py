"""Pod-state read tool — backup status.

Exposes ``pod_state.backup_status(bot_id)`` — everything the operator
needs to answer "is this bot backed up?" / "when did the last backup
run?" / "where is it being pushed to?" without resorting to a shell.

The fields returned mirror what an operator would otherwise have to
piece together by:

  1. ``cd /Users/<bot>/.openclaw/workspace && git log --grep='^\\[backup\\]' -1``
     → last_commit_at + last_commit_subject + last_commit_sha
  2. ``git remote get-url origin``
     → git_remote
  3. ``cat /Library/LaunchDaemons/ai.evolve.<bot>.backup.plist``
     and grep StartCalendarInterval
     → schedule
  4. ``jq .bots.<bot>.backupRepoUrl /Users/Shared/evolve/network.json``
     → configured_remote (what *should* be the remote)
  5. ``ls /Library/LaunchDaemons/ai.evolve.<bot>.backup.plist``
     → backup_configured (whether the bot has the daemon installed)

Centralizing those reads behind one tool keeps evo's answer grounded
in actual state rather than guessed, and avoids the "I'd need
elevated exec access" fabrication pattern when the operator asks a
question that doesn't have a registered tool answer (this one used to
not, leading to the 2026-05-19 transcript that motivated this tool).

Risk tier: ``read`` — pure inspection, no mutation. Returns immediately.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register
from .fs_tristate import ReadState, stat_status

log = logging.getLogger(__name__)


# ─── Path helpers ────────────────────────────────────────────────────────────


def _backup_plist_path(bot_id: str) -> Path:
    """LaunchDaemon plist installed by ``deploy._install_launchd_backup``."""
    return Path(f"/Library/LaunchDaemons/ai.evolve.{bot_id}.backup.plist")


def _workspace_path(bot_id: str) -> Path:
    """The bot's git workspace — analyzer/backup.py operates on this."""
    from ...config import bot_home
    return bot_home(bot_id) / ".openclaw" / "workspace"


def _stat_status(path: Path) -> str:
    """Three-way classification of a path's reachability.

    Returns ``"exists"`` when the path is statable, ``"absent"`` when the
    underlying file/dir is genuinely missing, and ``"indeterminate"`` when
    we can't tell because the OS denied the lookup (PermissionError, or
    any other OSError that isn't FileNotFoundError).

    Why this exists: ``Path.exists()`` collapses PermissionError into
    ``False``, which means cross-bot reads from the unprivileged ``evo``
    user (post-E.2.b cutover) produce a confidently-wrong "absent"
    answer for every bot whose ``.openclaw/`` is mode 700 — which is
    every bot. Callers that need the distinction (e.g. when picking
    between "report missing" vs "report unknown") must use this helper.

    Delegates to the shared ``fs_tristate.stat_status`` primitive so the
    errno classification (ENOENT→absent, EACCES/EPERM→indeterminate) lives
    in one place across every Evo bot-file read.
    """
    return stat_status(path).value


# ─── Git helpers ─────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, timeout: int = 5) -> subprocess.CompletedProcess:
    """Run a git subcommand in the bot's workspace.

    ``-c safe.directory=*`` matches what ``analyzer/backup.py`` does.
    The workspace is owned by the bot user; evolve reads it via the
    macOS ACL grant on ``.openclaw/`` (set by ``deploy.set_evolve_read_acl``).
    Without ``safe.directory=*``, git 2.35.2+ rejects cross-owner access
    with ``fatal: detected dubious ownership``.
    """
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _last_backup_commit(workspace: Path) -> dict[str, Any] | None:
    """Return metadata about the most recent ``[backup]``-prefixed commit.

    Returns ``None`` if the workspace isn't a git repo, has no backup
    commits yet, or git fails. Callers treat None as "no backup recorded
    in git yet" — the bot might have the daemon scheduled but never run.
    """
    if not workspace.exists():
        return None
    try:
        # Most recent commit whose subject starts with "[backup]".
        r = _git(
            ["log", "-1", "--grep", r"^\[backup\]",
             "--format=%H%x09%aI%x09%s"],
            workspace,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.strip().split("\t", 2)
    if len(parts) != 3:
        return None
    sha, iso_ts, subject = parts
    return {
        "last_commit_sha": sha[:12],
        "last_commit_at": iso_ts,
        "last_commit_subject": subject,
    }


def _git_remote(workspace: Path) -> str | None:
    """Return ``origin`` remote URL, or ``None`` if unset."""
    if not workspace.exists():
        return None
    try:
        r = _git(["remote", "get-url", "origin"], workspace)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


# ─── Plist helpers ───────────────────────────────────────────────────────────


def _read_plist_schedule(plist_path: Path) -> dict[str, Any] | None:
    """Read the StartCalendarInterval from the backup plist.

    Returns a dict like ``{"hour": 2, "minute": 0, "human": "daily at 02:00"}``,
    or ``None`` if the plist is missing or malformed. We don't surface
    every possible StartCalendarInterval shape (Weekday, Day, etc.) —
    the deploy code only ever installs Hour+Minute, so reading those
    two is enough for the operator's question.
    """
    if not plist_path.exists():
        return None
    try:
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    sci = data.get("StartCalendarInterval")
    if not isinstance(sci, dict):
        return None
    hour = sci.get("Hour")
    minute = sci.get("Minute")
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    return {
        "hour": hour,
        "minute": minute,
        "human": f"daily at {hour:02d}:{minute:02d}",
    }


# ─── Network helpers ─────────────────────────────────────────────────────────


def _configured_remote(network_path: Path, bot_id: str) -> str | None:
    """Return the ``backupRepoUrl`` for this bot from network.json.

    Distinct from the live git remote: when these disagree, the
    daemon's next run will reset the remote URL to the configured
    value (see ``analyzer/backup.py::_ensure_remote``). Surfacing both
    lets the operator catch a config-drift situation in one tool call.
    """
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
    except Exception:  # noqa: BLE001
        return None
    bots = net.get("bots") or {}
    bot_cfg = bots.get(bot_id) or {}
    url = bot_cfg.get("backupRepoUrl")
    return url if isinstance(url, str) and url else None


# ─── Handler ─────────────────────────────────────────────────────────────────


def _backup_status_handler(
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    """Inspect everything backup-related for one bot.

    Phase E.3.2 path: first try the admin daemon's unix-socket endpoint
    so cross-bot reads continue to work post-Phase-E.2.b (when evo's
    gateway runs as the unprivileged ``evo`` user with no cross-bot
    ACLs). Falls back to the legacy direct-fs path on socket failure —
    preserves operation during the migration window.

    Returns a flat dict the model can paraphrase directly. See the
    bottom-half of this function (the direct-fs path) for the field
    schema. Admin daemon route returns the same shape.
    """
    if not bot_id:
        return {"error": "bot_id is required"}

    # Primary path: HTTP over the admin daemon's unix socket.
    try:
        from ..admin_client import AdminDaemonUnavailable, get_json
        status, body = get_json(f"/api/admin/bot/{bot_id}/backup-status")
        if status == 200 and isinstance(body, dict):
            return body
        # Any other status (403, 404, 500) → fall through to direct-fs.
        log.warning(
            "pod_state.backup_status: admin daemon returned status %d for %s; "
            "falling back to direct-fs read",
            status, bot_id,
        )
    except AdminDaemonUnavailable as exc:
        # WARNING, not DEBUG — mute fallbacks hid the 2026-07-28 outage
        # for 10 days. log_daemon_fallback carries socket path + errno.
        from ..admin_client import log_daemon_fallback
        log_daemon_fallback(
            f"pod_state.backup_status GET /api/admin/bot/{bot_id}/backup-status",
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("pod_state.backup_status: admin daemon call raised: %s", exc)

    # Fallback path: legacy direct-fs reads. Pre-E.2.b this worked because
    # evo ran as the ``evolve`` user, which has macOS ACL read access to
    # every bot's ``.openclaw/``. Post-cutover (E.2.b) evo runs as the
    # unprivileged ``evo`` user with NO cross-bot ACL — the per-bot home
    # directories are mode 700 and stat() raises PermissionError.
    #
    # The hazard: ``Path.exists()`` swallows PermissionError and returns
    # False, indistinguishable from "directory genuinely missing." If we
    # blindly populated ``workspace_exists`` from that, evo would report
    # "the workspace doesn't exist!" for every bot it can't see, which is
    # a confidently-wrong claim that misleads the operator. Bit Pod_admin on
    # 2026-05-26 with all three of team_bot_c/personal_bot/security_bot being falsely
    # diagnosed as "workspace missing" when all three were healthy
    # (verified via the daemon endpoint which returned ``workspace_exists:
    # true`` with valid recent commits).
    #
    # Defense: ``_stat_status`` distinguishes "exists", "absent", and
    # "indeterminate". When the workspace stat returns "indeterminate" we
    # KNOW the daemon was the right answer and we don't have it — return
    # an error payload that's syntactically distinct (``ok: false``) from
    # the real status so consumers don't quietly treat absence as data.
    plist = _backup_plist_path(bot_id)
    workspace = _workspace_path(bot_id)

    workspace_status = _stat_status(workspace)
    if workspace_status == ReadState.INDETERMINATE.value:
        return {
            "ok": False,
            "bot_id": bot_id,
            "workspace_path": str(workspace),
            "error": (
                f"admin daemon unreachable AND direct fs read denied for "
                f"{bot_id}'s workspace; backup status unknown. Recover "
                f"the daemon with `sudo launchctl kickstart -k "
                f"system/ai.evolve.evolve.admin-ui` and retry. (Detail: "
                f"running as a user without macOS ACL access to "
                f"{workspace.parent}/, so the in-process fallback can't "
                f"see it.)"
            ),
            "fallback_blocked": True,
        }

    backup_configured = plist.exists()
    schedule = _read_plist_schedule(plist) if backup_configured else None
    workspace_exists = workspace_status == "exists"

    git_remote = _git_remote(workspace) if workspace_exists else None
    last_commit = _last_backup_commit(workspace) if workspace_exists else None
    configured_remote = _configured_remote(network_path, bot_id)

    never_run = backup_configured and workspace_exists and last_commit is None
    remote_drift = (
        configured_remote is not None
        and git_remote is not None
        and configured_remote != git_remote
    )

    out: dict[str, Any] = {
        "bot_id": bot_id,
        "backup_configured": backup_configured,
        "configured_remote": configured_remote,
        "schedule": schedule,
        "workspace_path": str(workspace),
        "workspace_exists": workspace_exists,
        "git_remote": git_remote,
        "last_commit_at": (last_commit or {}).get("last_commit_at"),
        "last_commit_sha": (last_commit or {}).get("last_commit_sha"),
        "last_commit_subject": (last_commit or {}).get("last_commit_subject"),
        "never_run": never_run,
        "remote_drift": remote_drift,
    }
    return out


BACKUP_STATUS_TOOL = Tool(
    name="pod_state.backup_status",
    description=(
        "Inspect git-backup state for one bot: whether the launchd "
        "daemon is installed (``ai.evolve.<bot>.backup``), its schedule, "
        "the configured remote (``network.json::bots.<bot>.backupRepoUrl``), "
        "the live git remote in the workspace, and the most recent "
        "``[backup]``-prefixed commit (timestamp + sha + subject). Pure "
        "read — no side effects. Use to answer 'when did team_bot_a last "
        "back up?', 'is the backup running?', 'where does it push to?', "
        "or as the verify_via after ``action.bot.backup_workspace`` "
        "(the last_commit_at field advances when the backup completes)."
    ),
    wire_description=(
        "Inspect one bot's git-backup state: whether the backup "
        "daemon is installed, its schedule, configured vs live git "
        "remote, and the most recent backup commit (timestamp, sha, "
        "subject). Pure read. Use for 'when did X last back up?', "
        "'where does it push?', or as verify_via after "
        "``action.bot.backup_workspace`` (last_commit_at advances)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_backup_status_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "backup", "read"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(BACKUP_STATUS_TOOL)
