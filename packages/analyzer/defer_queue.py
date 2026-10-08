#!/usr/bin/env python3
"""
defer_queue.py — Continuity Engine v2: per-bot deferral queue.

Replaces the post-hoc extraction model. Bots explicitly call the `defer` tool
(registered by the evolve plugin) when they commit to a future action; that
tool appends a row here. The defer_runner reads the queue every couple of
minutes and fires due rows.

One queue file per bot, written by both the bot itself (append, via the
plugin's tool) and the runner (rewrite to drop fired/failed rows). Both sides
take a file lock to avoid append/rewrite races. The bot has natural write
access to its own home; the evolve user has write access via the existing
read+write ACL set on `~/.openclaw/workspace/evolve/` by deploy.py
(`set_evolve_read_acl`).

Storage layout (per bot):
    /Users/<bot_id>/.openclaw/workspace/evolve/
        defer-queue.jsonl       — active rows
        defer-queue.jsonl.lock  — flock target
        defer-archive.jsonl     — terminal rows (fired/failed); append-only

Schema (v1):
    {
      "defer_id":      str,                  # uuid4
      "bot_id":        str,
      "channel_id":    str | None,           # for delivery
      "session_id":    str | None,           # ephemeral UUID — what `openclaw
                                             #   agent --session-id` accepts
      "session_key":   str | None,           # routing key (agent:main:telegram:...) —
                                             #   diagnostic only; CLI does NOT
                                             #   accept this format
      "fires_at":      str,                  # ISO 8601 UTC
      "created_at":    str,                  # ISO 8601 UTC
      "mode":          "message" | "action",
      "message":       str | None,           # mode=message: literal text
      "action":        str | None,           # mode=action: instruction for agent
      "status":        "pending" | "fired" | "failed",
      "fired_at":      str | None,
      "result":        str | None,
      "schema_version": 1,
    }
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import pwd
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


SCHEMA_VERSION = 1

MODE_MESSAGE = "message"
MODE_ACTION = "action"

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_FAILED = "failed"


# ── Path resolution ──────────────────────────────────────────────────────────


def bot_evolve_dir(bot_id: str) -> Path:
    """Per-bot evolve workspace directory. Bot writes here as itself; evolve
    reads/writes via the ACL grant from set_evolve_read_acl().

    Resolves the macOS account name via evolve_config.bot_home so that bots
    with a 'user' override in network.json (e.g. team_bot_b/personal_bot_user) are handled
    correctly. EVOLVE_DEFER_HOME_OVERRIDE bypasses resolution for tests.
    """
    override = os.environ.get("EVOLVE_DEFER_HOME_OVERRIDE")
    if override:
        return Path(override) / bot_id
    try:
        from evolve_config import bot_home as _bh
        return _bh(bot_id) / ".openclaw" / "workspace" / "evolve"
    except ImportError:
        import pwd
        try:
            return Path(pwd.getpwnam(bot_id).pw_dir) / ".openclaw" / "workspace" / "evolve"
        except KeyError:
            # Account doesn't exist yet — platform-keyed home root (/Users on
            # macOS, /home on Linux). A hardcoded /Users/ here wrote the queue
            # to a root-owned /Users/<bot> on Linux. (W10-G #5.)
            from platform_profile import get_profile
            return (Path(get_profile().user_home_root) / bot_id
                    / ".openclaw" / "workspace" / "evolve")


def queue_path(bot_id: str) -> Path:
    return bot_evolve_dir(bot_id) / "defer-queue.jsonl"


def archive_path(bot_id: str) -> Path:
    return bot_evolve_dir(bot_id) / "defer-archive.jsonl"


def lock_path(bot_id: str) -> Path:
    return bot_evolve_dir(bot_id) / "defer-queue.jsonl.lock"


# ── Row dataclass ────────────────────────────────────────────────────────────


@dataclasses.dataclass
class DeferRow:
    defer_id: str
    bot_id: str
    fires_at: str
    created_at: str
    mode: str
    channel_id: Optional[str] = None
    # Ephemeral session UUID — what `openclaw agent --session-id` accepts.
    # The runner uses this for dispatch.
    session_id: Optional[str] = None
    # Routing-style session key (e.g. `agent:main:telegram:direct:123456789`).
    # Diagnostic only — the CLI rejects this format with "Invalid session ID".
    session_key: Optional[str] = None
    message: Optional[str] = None
    action: Optional[str] = None
    status: str = STATUS_PENDING
    fired_at: Optional[str] = None
    result: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "DeferRow":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if self.status != STATUS_PENDING:
            return False
        ref = now or datetime.now(timezone.utc)
        try:
            t = datetime.fromisoformat(self.fires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t <= ref


# ── New row factory ──────────────────────────────────────────────────────────


def new_row(
    bot_id: str,
    fires_at: str,
    mode: str,
    *,
    message: Optional[str] = None,
    action: Optional[str] = None,
    channel_id: Optional[str] = None,
    session_id: Optional[str] = None,
    session_key: Optional[str] = None,
) -> DeferRow:
    if mode not in (MODE_MESSAGE, MODE_ACTION):
        raise ValueError(f"mode must be {MODE_MESSAGE!r} or {MODE_ACTION!r}, got {mode!r}")
    if mode == MODE_MESSAGE and not message:
        raise ValueError("mode=message requires non-empty message")
    if mode == MODE_ACTION and not action:
        raise ValueError("mode=action requires non-empty action")
    if mode == MODE_MESSAGE and action:
        raise ValueError("mode=message must not set action")
    if mode == MODE_ACTION and message:
        raise ValueError("mode=action must not set message")

    # Validate fires_at parses as ISO 8601
    try:
        datetime.fromisoformat(fires_at.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"fires_at not parseable as ISO 8601: {fires_at!r}") from e

    return DeferRow(
        defer_id=uuid.uuid4().hex,
        bot_id=bot_id,
        fires_at=fires_at,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode=mode,
        channel_id=channel_id,
        session_id=session_id,
        session_key=session_key,
        message=message,
        action=action,
    )


# ── Bot ownership preservation ───────────────────────────────────────────────


def _bot_uid_gid(bot_id: str) -> Optional[tuple[int, int]]:
    """Resolve (uid, gid) for the bot user, or None if the user doesn't exist
    on this system (e.g., test environments using EVOLVE_DEFER_HOME_OVERRIDE)."""
    try:
        pw = pwd.getpwnam(bot_id)
        return (pw.pw_uid, pw.pw_gid)
    except KeyError:
        return None


def _chown_to_bot(path: Path, bot_id: str) -> None:
    """Best-effort chown the file to the bot user. The defer runner runs as
    root, but the queue/archive files live in the bot's home and must remain
    bot-owned so the bot's gateway plugin (running as the bot user) can
    continue to append rows. Without this, root-created files get mode 600
    root:staff and the next defer call from the bot fails with EACCES.

    Silently no-ops when:
      - The bot user doesn't exist on this system (test environments)
      - We aren't root and can't chown anyway
      - The path doesn't exist
    """
    if not path.exists():
        return
    bot_uid_gid = _bot_uid_gid(bot_id)
    if bot_uid_gid is None:
        return
    try:
        os.chown(str(path), bot_uid_gid[0], bot_uid_gid[1])
    except (PermissionError, OSError):
        # Not root, or some other ownership issue. The original write
        # succeeded; this is best-effort hygiene. The next runner cycle
        # will retry the chown.
        pass


# Cross-user queue files: the bot (file owner) APPENDS via the defer tool and
# evolve (the runner) READS + REWRITES. The runner is the ``evolve`` service
# user, NOT root (the "runs as root" lineage in this module predates the
# evolve-account move — ai.openclaw.evolve.defer-runner has UserName=evolve), so
# it can neither chown nor write a file the inherited ACL doesn't reach.
#
# On Linux a file the bot creates at its restrictive umask (0600) zeroes the
# POSIX-ACL *mask*, and the mask caps the inherited ``u:evolve:rwX`` ACE to
# nothing — so the runner hit ``EACCES`` reading/rewriting the queue on the
# round-6 live fresh install (W10-G). Group-rw (0660) sets the mask to ``rw`` so
# the inherited cross-user ACE is effective. This is the same sharp edge the
# Perms seam documents for chmod, applied at the queue write path. Best-effort
# and owner-only: the bot owns the files it creates, the runner owns its rewrite
# tmp; a non-owner call (evolve touching a bot-owned file) no-ops. macOS:
# harmless mode bits — ACLs there are orthogonal to the mode, no mask.
_QUEUE_FILE_MODE = 0o660


def _ensure_cross_user_writable(path: Path) -> None:
    """chmod *path* group-rw (0660) so a Linux ACL mask can't clamp the
    inherited cross-user ACE. No-op when the group bits are already rw or when
    the caller doesn't own the file."""
    try:
        st = os.stat(str(path))
    except OSError:
        return
    if (st.st_mode & 0o060) == 0o060:
        return  # group already rw — the mask won't clamp the inherited ACE
    try:
        os.chmod(str(path), _QUEUE_FILE_MODE)
    except OSError:
        # Not the owner (e.g. the runner touching a bot-owned file) — the
        # owner-side write path sets the mode; this is best-effort.
        pass


# ── File lock ────────────────────────────────────────────────────────────────


@contextmanager
def _flock(bot_id: str):
    """Take an exclusive flock on the per-bot lockfile for the duration of the
    with-block. Creates the directory + lockfile if missing.

    Preserves bot ownership on the lockfile when invoked as root, so that
    subsequent bot-side LOCK_EX acquisitions can take the lock (which
    requires write access)."""
    d = bot_evolve_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    lf = lock_path(bot_id)
    created = not lf.exists()
    # Mode 0666 so the bot user has write access (required for LOCK_EX) even
    # when root creates the file. The chown to the bot happens immediately
    # after, but mode is set first to be safe.
    fd = os.open(str(lf), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if created:
            _chown_to_bot(lf, bot_id)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ── Read / write ─────────────────────────────────────────────────────────────


def read_queue(bot_id: str) -> list[DeferRow]:
    """Return the active queue (pending rows). Skips malformed lines."""
    p = queue_path(bot_id)
    if not p.exists():
        return []
    rows: list[DeferRow] = []
    with _flock(bot_id):
        with p.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(DeferRow.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
    return rows


def append_row(row: DeferRow) -> None:
    """Append a single row to the bot's queue. Used by the tool (runs as the
    bot, which owns the file)."""
    p = queue_path(row.bot_id)
    with _flock(row.bot_id):
        with p.open("a") as f:
            f.write(row.to_json() + "\n")
        # Group-rw so evolve's inherited ACL ACE isn't mask-clamped on Linux
        # (the runner reads + rewrites this file). See _ensure_cross_user_writable.
        _ensure_cross_user_writable(p)


def rewrite_queue(bot_id: str, rows: list[DeferRow]) -> None:
    """Atomically replace the bot's queue with the given rows. Used by the
    runner after firing some rows (which then go to archive).

    When invoked by the runner (running as root), preserves bot ownership on
    the resulting file. Without this, the rewritten file would be root-owned
    mode 600 and the next defer tool call from the bot's gateway would fail
    with EACCES — discovered during first integration test on 2026-05-05."""
    d = bot_evolve_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    p = queue_path(bot_id)
    with _flock(bot_id):
        # tempfile in same dir → atomic rename on POSIX
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".defer-queue.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for r in rows:
                    f.write(r.to_json() + "\n")
            # Set mode + ownership before rename, so the destination path is
            # never observed with the wrong permissions. 0660 (group-rw) keeps
            # the Linux ACL mask wide enough that the bot's inherited ACE (and
            # evolve's, on its own next pass) stays effective — see
            # _ensure_cross_user_writable.
            os.chmod(tmp, _QUEUE_FILE_MODE)
            _chown_to_bot(Path(tmp), bot_id)
            os.replace(tmp, str(p))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def append_archive(bot_id: str, row: DeferRow) -> None:
    """Append a terminal row to the archive. Append-only; no rewrite.

    Preserves bot ownership when run as root (see rewrite_queue rationale)."""
    p = archive_path(bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _flock(bot_id):
        existed_before = p.exists()
        with p.open("a") as f:
            f.write(row.to_json() + "\n")
        # On first creation, fix mode + ownership so subsequent bot-side
        # appends (if any) succeed. 0660 keeps the Linux ACL mask wide enough
        # for evolve's inherited read ACE (the runner reads the archive too).
        if not existed_before:
            os.chmod(str(p), _QUEUE_FILE_MODE)
            _chown_to_bot(p, bot_id)


# ── Multi-bot iteration ──────────────────────────────────────────────────────


def iter_bot_queues(bot_ids: list[str]) -> Iterator[tuple[str, list[DeferRow]]]:
    """Yield (bot_id, rows) for each bot that has a queue file."""
    for bot_id in bot_ids:
        if not queue_path(bot_id).exists():
            continue
        yield bot_id, read_queue(bot_id)


def list_due(bot_ids: list[str], now: Optional[datetime] = None) -> list[DeferRow]:
    """Flat list of all currently due pending rows across the given bots."""
    out: list[DeferRow] = []
    for _, rows in iter_bot_queues(bot_ids):
        out.extend(r for r in rows if r.is_due(now))
    return out
