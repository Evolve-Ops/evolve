"""
better_engine/pending_tasks.py — Pending admin task queue (§19.5).

File location per bot:
    /Users/{bot_id}/.openclaw/workspace/evolve/pending-admin-tasks.json

Written by the bot via the Better Engine API.  The evolve user reads these
files directly (ACL grant on workspace/evolve/).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PendingTask:
    id: str                          # pat_{epoch}_{short_hash}
    rec_id: str
    dedup_key: str
    type: str
    title: str
    detail: str
    context: str
    action: str
    action_args: dict
    bot_id: str
    queued_at: str
    resolved_at: Optional[str] = None
    resolved_how: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rec_id": self.rec_id,
            "dedup_key": self.dedup_key,
            "type": self.type,
            "title": self.title,
            "detail": self.detail,
            "context": self.context,
            "action": self.action,
            "action_args": self.action_args,
            "bot_id": self.bot_id,
            "queued_at": self.queued_at,
            "resolved_at": self.resolved_at,
            "resolved_how": self.resolved_how,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingTask":
        return cls(
            id=d["id"],
            rec_id=d.get("rec_id", ""),
            dedup_key=d.get("dedup_key", ""),
            type=d.get("type", ""),
            title=d.get("title", ""),
            detail=d.get("detail", ""),
            context=d.get("context", ""),
            action=d.get("action", ""),
            action_args=d.get("action_args", {}),
            bot_id=d.get("bot_id", ""),
            queued_at=d.get("queued_at", ""),
            resolved_at=d.get("resolved_at"),
            resolved_how=d.get("resolved_how"),
        )


# ── File path helpers ─────────────────────────────────────────────────────────

def pending_tasks_path(bot_workspace_dir: Path) -> Path:
    """Return the path to pending-admin-tasks.json for a bot workspace."""
    return bot_workspace_dir / "evolve" / "pending-admin-tasks.json"


# ── Load / save ───────────────────────────────────────────────────────────────

def load_pending_tasks(path: Path) -> list[PendingTask]:
    """Load pending tasks from file; returns [] if missing or corrupt."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        tasks_raw = data.get("tasks", [])
        return [PendingTask.from_dict(t) for t in tasks_raw]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def save_pending_tasks(tasks: list[PendingTask], path: Path) -> None:
    """Atomically write pending tasks to JSON file.

    The workspace/evolve/ directory has write ACL for the evolve user, so
    direct writes work without sudo.
    """
    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Determine bot_id from the first task or from path structure
    bot_id = tasks[0].bot_id if tasks else ""

    data = {
        "schema_version": 1,
        "bot_id": bot_id,
        "tasks": [t.to_dict() for t in tasks],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── Task operations ───────────────────────────────────────────────────────────

def _generate_task_id(rec_id: str, dedup_key: str) -> str:
    """Generate a stable task ID."""
    epoch = int(time.time())
    raw = f"{rec_id}:{dedup_key}:{epoch}"
    short_hash = hashlib.sha1(raw.encode()).hexdigest()[:6]
    return f"pat_{epoch}_{short_hash}"


def add_task(
    bot_id: str,
    rec_id: str,
    dedup_key: str,
    type: str,
    title: str,
    detail: str,
    context: str,
    action: str,
    action_args: dict,
    path: Path,
) -> PendingTask:
    """Create a new pending task and append it to the file.

    Returns the newly created PendingTask.
    """
    from .model import now_iso

    tasks = load_pending_tasks(path)

    task = PendingTask(
        id=_generate_task_id(rec_id, dedup_key),
        rec_id=rec_id,
        dedup_key=dedup_key,
        type=type,
        title=title,
        detail=detail,
        context=context,
        action=action,
        action_args=action_args,
        bot_id=bot_id,
        queued_at=now_iso(),
    )
    tasks.append(task)
    save_pending_tasks(tasks, path)
    return task


def resolve_task(
    task_id: str,
    how: str,
    path: Path,
) -> Optional[PendingTask]:
    """Mark a task resolved (sets resolved_at and resolved_how), saves file.

    Returns the updated task, or None if not found.
    how: "admin_completed" | "admin_dismissed" | "auto_resolved"
    """
    from .model import now_iso

    tasks = load_pending_tasks(path)
    target: Optional[PendingTask] = None
    for task in tasks:
        if task.id == task_id:
            task.resolved_at = now_iso()
            task.resolved_how = how
            target = task
            break

    if target is None:
        return None

    save_pending_tasks(tasks, path)
    return target


def get_all_pending_tasks(
    bot_workspace_dirs: dict[str, Path],
) -> list[PendingTask]:
    """Read all bots' files; return all unresolved tasks sorted by queued_at.

    bot_workspace_dirs is {bot_id: workspace_path}.
    """
    all_tasks: list[PendingTask] = []
    for bot_id, workspace_dir in bot_workspace_dirs.items():
        path = pending_tasks_path(workspace_dir)
        tasks = load_pending_tasks(path)
        for task in tasks:
            if task.resolved_at is None:
                all_tasks.append(task)

    all_tasks.sort(key=lambda t: t.queued_at)
    return all_tasks
