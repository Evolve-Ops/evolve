#!/usr/bin/env python3
"""
manifest_reflex_queue.py — Per-bot queue of self-reported app manifests.

Bots call the `record_application` plugin tool when they ship something
app-shaped (a script, a cron, a tracker). That tool appends a row to this
queue. The manifest-reflex runner reads it every cycle, calls
``evolve_admin.applications.manifest.save_manifest`` to land each row in
``{shared_dir}/applications/{bot_id}/``, and rewrites the queue to drop
processed rows.

Mirrors the defer_queue pattern: per-bot JSONL under
``/Users/<bot_id>/.openclaw/workspace/evolve/`` (which both the bot and the
evolve user can write to via deploy.py's ACL grant), with a flock for
append/rewrite races.

Storage layout (per bot):
    /Users/<bot_id>/.openclaw/workspace/evolve/
        manifest-reflex-queue.jsonl       — pending rows
        manifest-reflex-queue.jsonl.lock  — flock target
        manifest-reflex-archive.jsonl     — terminal rows; append-only

Schema (v1):
    {
      "reflex_id":      str,                  # uuid4 hex
      "bot_id":         str,
      "session_id":     str | None,           # for source_detail
      "session_key":    str | None,           # diagnostic only
      "created_at":     str,                  # ISO 8601 UTC
      "app_id":         str,                  # manifest slug
      "name":           str,                  # human-readable
      "purpose":        str,                  # 1-2 sentence why
      "files":          list[str],            # workspace-relative or absolute paths
      "crons":          list[dict],           # [{schedule, script}, ...]
      "inputs":         list[str],
      "outputs":        list[str],
      "test_command":   str,                  # optional; "" means none
      "update":         bool,                 # true → patch existing manifest
      "status":         "pending"|"applied"|"failed",
      "applied_at":     str | None,
      "result":         str | None,
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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"


# ── Path resolution ──────────────────────────────────────────────────────────


def bot_evolve_dir(bot_id: str) -> Path:
    """Per-bot evolve workspace directory. Bot writes here as itself; evolve
    reads/writes via the ACL grant from set_evolve_read_acl(). The override
    env var lets tests redirect to a tmpdir without touching real bot homes;
    it's intentionally the same name as defer_queue uses (EVOLVE_DEFER_HOME_OVERRIDE)
    so a single override redirects every bot-side queue in tests.

    Resolves the macOS account name via evolve_config.bot_home so that bots
    with a 'user' override in network.json (e.g. team_bot_b/personal_bot_user) are handled
    correctly.
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
    return bot_evolve_dir(bot_id) / "manifest-reflex-queue.jsonl"


def archive_path(bot_id: str) -> Path:
    return bot_evolve_dir(bot_id) / "manifest-reflex-archive.jsonl"


def lock_path(bot_id: str) -> Path:
    return bot_evolve_dir(bot_id) / "manifest-reflex-queue.jsonl.lock"


# ── Row dataclass ────────────────────────────────────────────────────────────


@dataclasses.dataclass
class ReflexRow:
    reflex_id: str
    bot_id: str
    created_at: str
    app_id: str
    name: str = ""
    purpose: str = ""
    session_id: Optional[str] = None
    session_key: Optional[str] = None
    files: list = dataclasses.field(default_factory=list)
    crons: list = dataclasses.field(default_factory=list)
    inputs: list = dataclasses.field(default_factory=list)
    outputs: list = dataclasses.field(default_factory=list)
    test_command: str = ""
    update: bool = False
    status: str = STATUS_PENDING
    applied_at: Optional[str] = None
    result: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "ReflexRow":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Bot ownership preservation ───────────────────────────────────────────────


def _bot_uid_gid(bot_id: str) -> Optional[tuple[int, int]]:
    try:
        pw = pwd.getpwnam(bot_id)
        return (pw.pw_uid, pw.pw_gid)
    except KeyError:
        return None


def _chown_to_bot(path: Path, bot_id: str) -> None:
    """Best-effort chown to the bot user. The runner runs as root, but the
    queue/archive files live in the bot's home and must remain bot-owned so
    the bot's plugin (running as the bot user) can keep appending rows.
    Without this, root-created files get mode 600 root:staff and the next
    record_application call from the bot fails with EACCES — same failure
    mode caught for defer_queue on 2026-05-05."""
    if not path.exists():
        return
    bot_uid_gid = _bot_uid_gid(bot_id)
    if bot_uid_gid is None:
        return
    try:
        os.chown(str(path), bot_uid_gid[0], bot_uid_gid[1])
    except (PermissionError, OSError):
        pass


# Cross-user queue files: the bot (file owner) appends via the TS tool and
# evolve (the manifest-reflex-runner, a non-root service user) reads + rewrites.
# On Linux a file created at the bot's umask (0600) zeroes the POSIX-ACL mask,
# capping the inherited u:evolve ACE to nothing → the runner hit EACCES on the
# round-6 live fresh install (W10-G). Group-rw (0660) keeps the mask wide enough
# for the cross-user ACE. The TS writer (RecordApplicationTool) sets the same
# mode at the real bot-side create; this mirrors it for the Python rewrite tmp +
# the test/backfill append path. macOS: harmless mode bits (ACLs are orthogonal).
_QUEUE_FILE_MODE = 0o660


def _ensure_cross_user_writable(path: Path) -> None:
    """chmod *path* group-rw (0660) so a Linux ACL mask can't clamp the
    inherited cross-user ACE. No-op when group bits are already rw or the caller
    doesn't own the file."""
    try:
        st = os.stat(str(path))
    except OSError:
        return
    if (st.st_mode & 0o060) == 0o060:
        return
    try:
        os.chmod(str(path), _QUEUE_FILE_MODE)
    except OSError:
        pass


# ── File lock ────────────────────────────────────────────────────────────────


@contextmanager
def _flock(bot_id: str):
    """Take an exclusive flock on the per-bot lockfile. Mirrors defer_queue's
    pattern: mode 0666 on the lockfile so the bot user can also acquire
    LOCK_EX (which requires write access on the file)."""
    d = bot_evolve_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    lf = lock_path(bot_id)
    created = not lf.exists()
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


def read_queue(bot_id: str) -> list[ReflexRow]:
    """Return all pending rows. Skips malformed lines."""
    p = queue_path(bot_id)
    if not p.exists():
        return []
    rows: list[ReflexRow] = []
    with _flock(bot_id):
        with p.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(ReflexRow.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
    return rows


def append_row(row: ReflexRow) -> None:
    """Append a single row to the bot's queue. Used by the tool (TS-side
    via direct fs.appendFileSync; this Python helper is used by tests and
    backfill)."""
    p = queue_path(row.bot_id)
    with _flock(row.bot_id):
        with p.open("a") as f:
            f.write(row.to_json() + "\n")
        _ensure_cross_user_writable(p)


def rewrite_queue(bot_id: str, rows: list[ReflexRow]) -> None:
    """Atomically replace the bot's queue with the given rows. Used by the
    runner after applying some rows (which then go to archive). Preserves
    bot ownership when running as root."""
    d = bot_evolve_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    p = queue_path(bot_id)
    with _flock(bot_id):
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".manifest-reflex-queue.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for r in rows:
                    f.write(r.to_json() + "\n")
            # 0660 (group-rw) keeps the Linux ACL mask wide enough that the
            # inherited cross-user ACE stays effective — see
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


def append_archive(bot_id: str, row: ReflexRow) -> None:
    """Append a terminal row to the archive. Append-only; no rewrite."""
    p = archive_path(bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _flock(bot_id):
        existed_before = p.exists()
        with p.open("a") as f:
            f.write(row.to_json() + "\n")
        if not existed_before:
            os.chmod(str(p), _QUEUE_FILE_MODE)
            _chown_to_bot(p, bot_id)


# ── Multi-bot iteration ──────────────────────────────────────────────────────


def iter_bot_queues(bot_ids: list[str]) -> Iterator[tuple[str, list[ReflexRow]]]:
    """Yield (bot_id, rows) for each bot that has a queue file."""
    for bot_id in bot_ids:
        if not queue_path(bot_id).exists():
            continue
        yield bot_id, read_queue(bot_id)


# ── Row factory (used by tests + the Python tool fallback) ───────────────────


def new_row(
    bot_id: str,
    app_id: str,
    *,
    name: str = "",
    purpose: str = "",
    files: Optional[list] = None,
    crons: Optional[list] = None,
    inputs: Optional[list] = None,
    outputs: Optional[list] = None,
    test_command: str = "",
    update: bool = False,
    session_id: Optional[str] = None,
    session_key: Optional[str] = None,
) -> ReflexRow:
    if not bot_id or not app_id:
        raise ValueError("bot_id and app_id are required")
    import uuid
    return ReflexRow(
        reflex_id=uuid.uuid4().hex,
        bot_id=bot_id,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        app_id=app_id,
        name=name or app_id,
        purpose=purpose,
        session_id=session_id,
        session_key=session_key,
        files=list(files or []),
        crons=list(crons or []),
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        test_command=test_command or "",
        update=bool(update),
    )
