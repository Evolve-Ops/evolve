"""arbiter.appliers.workflow_instruction — Write a workflow markdown file.

WorkflowInstruction writes a markdown instruction file into the bot's
workspace. Paths are constrained to the bot's workspace via a prefix check;
attempts to escape via ``..`` or absolute paths outside the workspace are
rejected.

Snapshot captures the file's prior content (if any) so revert can restore
it. If the file didn't exist before apply, revert deletes the written file.
"""

from __future__ import annotations

import os
from pathlib import Path

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import WorkflowInstruction

from evolve_config import bot_home as _bot_home


def _resolve_workflow_path(bot_id: str, rel_path: str) -> Path:
    """Compute the absolute workflow file path and guard against escape."""
    base = _bot_home(bot_id) / ".openclaw" / "workspace"
    # Normalize the relative path
    rel = Path(rel_path)
    if rel.is_absolute():
        raise ValueError(
            f"WorkflowInstruction.path must be relative to the bot workspace; "
            f"got absolute path {rel_path!r}"
        )
    target = (base / rel).resolve()
    if not str(target).startswith(str(base.resolve()) + os.sep) and target != base.resolve():
        raise ValueError(
            f"WorkflowInstruction.path escapes the bot workspace: {rel_path!r}"
        )
    return target


class WorkflowInstructionApplier:
    """Writes markdown instruction files scoped to a bot's workspace."""

    def capture_snapshot(
        self, action: WorkflowInstruction, bot_id: str
    ) -> dict:
        target = _resolve_workflow_path(action.bot_id or bot_id, action.path)
        existed = target.exists()
        prior_content: str | None = None
        if existed:
            try:
                prior_content = target.read_text(encoding="utf-8")
            except OSError:
                # If we can't read, we can't promise a clean revert. Surface
                # as snapshot with None content; revert will try best-effort.
                prior_content = None
        return {
            "action_kind": "WorkflowInstruction",
            "bot_id": action.bot_id or bot_id,
            "path": str(target),
            "existed_before": existed,
            "prior_content": prior_content,
        }

    def apply(
        self, action: WorkflowInstruction, bot_id: str
    ) -> ApplyResult:
        target = _resolve_workflow_path(action.bot_id or bot_id, action.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(action.content, encoding="utf-8")
        return ApplyResult(
            ok=True,
            details={"path": str(target), "bytes_written": len(action.content)},
            message=f"Wrote {target}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        path = Path(snapshot.get("path", ""))
        existed_before = bool(snapshot.get("existed_before"))
        prior_content = snapshot.get("prior_content")

        if not path or not path.name:
            return RevertResult(
                ok=False,
                message="snapshot missing path — cannot revert",
            )

        if not existed_before:
            # Apply created the file; revert deletes it
            try:
                if path.exists():
                    path.unlink()
                return RevertResult(
                    ok=True,
                    details={"path": str(path), "deleted": True},
                    message=f"Deleted {path} (did not exist before apply)",
                )
            except OSError as e:
                return RevertResult(
                    ok=False,
                    details={"error": str(e)},
                    message=f"Failed to delete {path}: {e}",
                )

        # File existed before — restore prior content
        if prior_content is None:
            return RevertResult(
                ok=False,
                message=(
                    f"file {path} existed before apply but prior content was not captured; "
                    "manual intervention required"
                ),
            )
        try:
            path.write_text(prior_content, encoding="utf-8")
        except OSError as e:
            return RevertResult(
                ok=False,
                details={"error": str(e)},
                message=f"Failed to restore {path}: {e}",
            )
        return RevertResult(
            ok=True,
            details={"path": str(path), "restored": True},
            message=f"Restored {path} to prior content",
        )


register_applier("WorkflowInstruction", WorkflowInstructionApplier())
