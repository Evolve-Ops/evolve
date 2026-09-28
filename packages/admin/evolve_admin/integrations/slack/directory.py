"""Slack workspace identity directory.

Solves the team_bot_a-2026-05-15 confusion: team_bot_a didn't recognize that
``"pod_admin"`` (the Slack legacy username) and ``"Pod_admin"`` (the real
name) and ``U0PLKKXV0`` (the Slack ID) all referred to the same
person. The fix is to maintain a curated workspace directory that
maps every active user's full identity tuple, and inject a compact
form of it into the bot's system prompt at session_start.

Storage layout::

    {shared_dir}/bots/<bot_id>/slack-directory.json

Schema (Phase 1)::

    {
      "schema_version": 1,
      "bot_id": "team_bot_a",
      "team_id": "T0PKH8UH1",
      "team_name": "Example Corp",
      "last_refreshed_at": "2026-05-15T15:30:00Z",
      "users_read_email_scope": false,
      "user_count": 30,
      "users": [
        {
          "id": "U0PLKKXV0",
          "name": "pod_admin",
          "real_name": "Pod_admin Alden",
          "display_name": null,
          "email": null,
          "title": null,
          "is_admin": true,
          "is_owner": true,
          "is_bot": false,
          "deleted": false,
          "tz": "America/Los_Angeles"
        }, ...
      ]
    }

Atomic write via ``os.replace`` (same pattern as
:mod:`evolve_admin.integrations.slack.policy`). Owned by the
``evolve`` user — the shared dir already has the right ACL, no
sudo dance needed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _now_iso

from .slack_client import SlackClient, SlackError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DIRECTORY_FILENAME = "slack-directory.json"
DEFAULT_STALE_AFTER_HOURS = 24


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class UserRecord:
    """One workspace user's identity tuple.

    ``email`` is populated only when the bot has the
    ``users:read.email`` scope; otherwise ``None``. The doctor's
    SLK018 advises operators to add the scope so the directory is
    fully useful.
    """
    id: str
    name: str | None = None              # legacy username (e.g. "pod_admin")
    real_name: str | None = None         # canonical full name (e.g. "Pod_admin Alden")
    display_name: str | None = None      # operator-chosen workspace display
    email: str | None = None
    title: str | None = None
    is_admin: bool = False
    is_owner: bool = False
    is_bot: bool = False
    deleted: bool = False
    tz: str | None = None

    def role_label(self) -> str:
        """One-line role summary for the injected markdown table."""
        if self.deleted:
            return "deactivated"
        if self.is_bot:
            return "bot"
        bits: list[str] = []
        if self.is_owner:
            bits.append("owner")
        if self.is_admin and not self.is_owner:
            bits.append("admin")
        return "/".join(bits) if bits else "member"


@dataclass
class WorkspaceDirectory:
    """The whole on-disk directory record."""
    bot_id: str
    team_id: str = ""
    team_name: str = ""
    last_refreshed_at: str = ""
    users_read_email_scope: bool = False
    users: list[UserRecord] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def user_count(self) -> int:
        return len(self.users)


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────


def directory_path(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "bots" / bot_id / DIRECTORY_FILENAME


def load_directory(shared_dir: Path, bot_id: str) -> WorkspaceDirectory | None:
    """Return the stored directory, or ``None`` if absent.

    Raises ``ValueError`` on a present-but-malformed file — callers
    should treat that as a hard error (the doctor surfaces it
    explicitly).
    """
    path = directory_path(shared_dir, bot_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"directory file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"directory file {path} root is not an object")

    users: list[UserRecord] = []
    for u in (data.get("users") or []):
        if not isinstance(u, dict) or not isinstance(u.get("id"), str):
            continue
        users.append(UserRecord(
            id=u["id"],
            name=u.get("name") or None,
            real_name=u.get("real_name") or None,
            display_name=u.get("display_name") or None,
            email=u.get("email") or None,
            title=u.get("title") or None,
            is_admin=bool(u.get("is_admin", False)),
            is_owner=bool(u.get("is_owner", False)),
            is_bot=bool(u.get("is_bot", False)),
            deleted=bool(u.get("deleted", False)),
            tz=u.get("tz") or None,
        ))
    return WorkspaceDirectory(
        bot_id=str(data.get("bot_id") or bot_id),
        team_id=str(data.get("team_id") or ""),
        team_name=str(data.get("team_name") or ""),
        last_refreshed_at=str(data.get("last_refreshed_at") or ""),
        users_read_email_scope=bool(data.get("users_read_email_scope", False)),
        users=users,
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
    )


def save_directory(shared_dir: Path, directory: WorkspaceDirectory) -> Path:
    """Atomically write the directory. Returns the final path."""
    path = directory_path(shared_dir, directory.bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": directory.schema_version,
        "bot_id": directory.bot_id,
        "team_id": directory.team_id,
        "team_name": directory.team_name,
        "last_refreshed_at": directory.last_refreshed_at,
        "users_read_email_scope": directory.users_read_email_scope,
        "user_count": directory.user_count,
        "users": [asdict(u) for u in directory.users],
    }
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{DIRECTORY_FILENAME}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Refresh
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RefreshResult:
    ok: bool
    bot_id: str
    user_count: int = 0
    team_name: str = ""
    team_id: str = ""
    saved_path: Path | None = None
    users_read_email_scope: bool = False
    error: str | None = None


def refresh_workspace_directory(
    bot_id: str,
    *,
    bot_token: str,
    shared_dir: Path,
    slack_client: Any | None = None,
) -> RefreshResult:
    """Pull the workspace user list + save the directory to disk.

    Filters:
    - Skips ``is_bot`` users (workspace bots / app accounts aren't
      people the bot ever needs to disambiguate by name).
    - Keeps deactivated users (``deleted=true``) so historical messages
      can still resolve names. The role_label marks them as "deactivated"
      so the bot knows they're inactive.

    ``slack_client`` is injected for tests; production callers pass
    ``bot_token`` and we construct a real client.
    """
    if slack_client is None:
        try:
            slack_client = SlackClient(bot_token)
        except ValueError as exc:
            return RefreshResult(
                ok=False, bot_id=bot_id, error=f"invalid_token: {exc}",
            )

    auth, auth_err = slack_client.auth_test()
    if auth_err is not None or not isinstance(auth, dict):
        return RefreshResult(
            ok=False, bot_id=bot_id,
            error=f"auth_test_failed: {auth_err}",
        )
    team_id = str(auth.get("team_id") or "")
    team_name = str(auth.get("team") or "")
    scopes = auth.get("_scopes") or []
    has_email_scope = "users:read.email" in scopes if isinstance(scopes, list) else False

    members, list_err = slack_client.users_list()
    if list_err is not None or not isinstance(members, list):
        return RefreshResult(
            ok=False, bot_id=bot_id,
            error=f"users_list_failed: {list_err}",
            team_id=team_id, team_name=team_name,
        )

    users: list[UserRecord] = []
    for m in members:
        if not isinstance(m, dict):
            continue
        uid = m.get("id")
        if not isinstance(uid, str):
            continue
        if m.get("is_bot"):
            continue  # not a person; skip
        profile = m.get("profile") or {}
        if not isinstance(profile, dict):
            profile = {}
        users.append(UserRecord(
            id=uid,
            name=m.get("name") or None,
            real_name=m.get("real_name") or profile.get("real_name") or None,
            display_name=profile.get("display_name") or None,
            email=profile.get("email") or None,
            title=profile.get("title") or None,
            is_admin=bool(m.get("is_admin", False)),
            is_owner=bool(m.get("is_owner", False)),
            is_bot=bool(m.get("is_bot", False)),
            deleted=bool(m.get("deleted", False)),
            tz=m.get("tz") or None,
        ))

    # Sort by canonical name for stable output; admins float to the top
    # within the alphabetical order so the table reads nicely.
    users.sort(key=lambda u: (
        not (u.is_owner or u.is_admin),
        (u.real_name or u.name or u.id).lower(),
    ))

    directory = WorkspaceDirectory(
        bot_id=bot_id,
        team_id=team_id,
        team_name=team_name,
        last_refreshed_at=_now_iso(),
        users_read_email_scope=bool(has_email_scope),
        users=users,
    )
    try:
        saved = save_directory(shared_dir, directory)
    except (ValueError, OSError) as exc:
        return RefreshResult(
            ok=False, bot_id=bot_id,
            error=f"save_failed: {exc}",
            team_id=team_id, team_name=team_name,
        )
    return RefreshResult(
        ok=True, bot_id=bot_id,
        user_count=len(users),
        team_id=team_id, team_name=team_name,
        users_read_email_scope=bool(has_email_scope),
        saved_path=saved,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering (for session-prompt injection + CLI display)
# ─────────────────────────────────────────────────────────────────────────────


# Hard cap for the system-prompt injection. Matches the
# ``app_posture`` block's cap so we don't blow the bot's prompt
# budget. 30-40 users fit comfortably; bigger workspaces get
# truncated with a tail note pointing to the on-disk file.
INJECTION_CHAR_CAP = 3000


def render_directory_markdown(
    directory: WorkspaceDirectory,
    *,
    include_email: bool = True,
    cap_chars: int = INJECTION_CHAR_CAP,
) -> str:
    """Render the directory as a compact markdown block.

    Designed for two consumers:

    - The session-prompt injection (capped at ``cap_chars``).
    - The CLI ``slack-directory <bot>`` --show output (uncapped).

    The preamble names the canonical identity rule explicitly so the
    bot doesn't have to infer it.
    """
    if not directory.users:
        return ""
    team_label = directory.team_name or directory.team_id or "workspace"
    has_email = include_email and directory.users_read_email_scope
    header_cols = ["Slack ID", "name", "display_name", "real_name", "role"]
    if has_email:
        header_cols.insert(4, "email")

    lines: list[str] = []
    lines.append(f"## Slack workspace identities ({team_label})")
    lines.append("")
    lines.append(
        "Every row below is one person. If you see any of "
        "(Slack ID, `name`, `display_name`, `real_name`"
        + (", `email`" if has_email else "")
        + ") in a message envelope or body, all of those identify "
        "the same individual. Don't ask the user to disambiguate "
        "between them — they're already the same."
    )
    lines.append("")
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("|" + "|".join(["---"] * len(header_cols)) + "|")

    for u in directory.users:
        row = [
            u.id,
            u.name or "—",
            u.display_name or "—",
            u.real_name or "—",
        ]
        if has_email:
            row.append(u.email or "—")
        row.append(u.role_label())
        lines.append("| " + " | ".join(row) + " |")

    if directory.last_refreshed_at:
        lines.append("")
        lines.append(
            f"_Refreshed {directory.last_refreshed_at}. "
            f"To refresh manually: "
            f"`evolve-admin slack-directory {directory.bot_id} --refresh`._"
        )

    body = "\n".join(lines)
    if len(body) > cap_chars:
        # Truncate at the previous full row boundary so we don't
        # leave a half-rendered user. We keep the preamble + header
        # then as many rows as fit, then add a tail note.
        cutoff = body.rfind("\n|", 0, cap_chars)
        if cutoff > 0:
            truncated = body[:cutoff].rstrip()
            kept_rows = truncated.count("\n| U")
            note = (
                f"\n\n_({directory.user_count - kept_rows} more users truncated "
                f"for systemAppend; full directory at "
                f"`{{shared_dir}}/bots/{directory.bot_id}/{DIRECTORY_FILENAME}`)_"
            )
            body = truncated + note
    return body


# ─────────────────────────────────────────────────────────────────────────────
# Staleness check (used by SLK017)
# ─────────────────────────────────────────────────────────────────────────────


def directory_age_hours(directory: WorkspaceDirectory) -> float | None:
    """Hours since ``last_refreshed_at``, or ``None`` if unparseable."""
    if not directory.last_refreshed_at:
        return None
    try:
        # Accept the strict ISO Z form we emit + a fallback parse for safety
        ts = directory.last_refreshed_at.rstrip("Z")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 3600.0


def is_stale(
    directory: WorkspaceDirectory,
    *,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> bool:
    age = directory_age_hours(directory)
    if age is None:
        return True  # unknown timestamp → treat as stale
    return age > stale_after_hours


__all__ = [
    "DEFAULT_STALE_AFTER_HOURS",
    "DIRECTORY_FILENAME",
    "INJECTION_CHAR_CAP",
    "RefreshResult",
    "SCHEMA_VERSION",
    "UserRecord",
    "WorkspaceDirectory",
    "directory_age_hours",
    "directory_path",
    "is_stale",
    "load_directory",
    "refresh_workspace_directory",
    "render_directory_markdown",
    "save_directory",
]
