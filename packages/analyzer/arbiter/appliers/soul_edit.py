"""SoulEdit applier — append/edit SOUL.md or AGENTS.md.

Spec §4.4. Always human-approval; applier runs only after approved_human.

Implementation details:
  - append_section: inserts a new section at end of the document
  - replace_section: replaces the block under the given anchor heading
  - refine_line: replaces a matched line (exact match) with new content

Snapshot captures the entire file's prior content so revert is exact.
"""

from __future__ import annotations

from pathlib import Path

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from evolve_config import bot_home as _bot_home
from schema.proposal import SoulEdit


def _target_path(bot_id: str, target: str) -> Path:
    filename = "SOUL.md" if target == "soul" else "AGENTS.md"
    return _bot_home(bot_id) / ".openclaw" / "workspace" / filename


class SoulEditApplier:
    def capture_snapshot(self, action: SoulEdit, bot_id: str) -> dict:
        path = _target_path(action.bot_id or bot_id, action.target)
        prior = ""
        if path.exists():
            try:
                prior = path.read_text(encoding="utf-8")
            except OSError:
                prior = ""
        return {
            "action_kind": "SoulEdit",
            "path": str(path),
            "prior_content": prior,
            "existed_before": path.exists(),
        }

    def apply(self, action: SoulEdit, bot_id: str) -> ApplyResult:
        path = _target_path(action.bot_id or bot_id, action.target)
        prior = ""
        if path.exists():
            try:
                prior = path.read_text(encoding="utf-8")
            except OSError as e:
                return ApplyResult(ok=False, message=f"read failed: {e}")

        try:
            new_content = _apply_operation(
                prior,
                operation=action.operation,
                content=action.content,
                anchor=action.anchor,
            )
        except ValueError as e:
            return ApplyResult(ok=False, message=str(e))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return ApplyResult(ok=False, message=f"write failed: {e}")

        return ApplyResult(
            ok=True,
            details={"path": str(path), "operation": action.operation},
            message=f"{action.operation} on {path.name}",
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        path = Path(snapshot.get("path", ""))
        prior = snapshot.get("prior_content", "")
        existed = bool(snapshot.get("existed_before"))
        if not existed:
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                return RevertResult(ok=False, message=f"delete failed: {e}")
            return RevertResult(ok=True, message=f"deleted {path}")
        try:
            path.write_text(prior, encoding="utf-8")
        except OSError as e:
            return RevertResult(ok=False, message=f"restore failed: {e}")
        return RevertResult(ok=True, message=f"restored {path}")


def _apply_operation(
    prior: str,
    *,
    operation: str,
    content: str,
    anchor: str | None,
) -> str:
    if operation == "append_section":
        if not prior or prior.endswith("\n"):
            separator = ""
        else:
            separator = "\n"
        return prior + separator + "\n" + content.rstrip() + "\n"

    if operation == "replace_section":
        if not anchor:
            raise ValueError("replace_section requires an anchor")
        return _replace_section(prior, anchor, content)

    if operation == "refine_line":
        if not anchor:
            raise ValueError("refine_line requires an anchor")
        if anchor not in prior:
            raise ValueError(f"anchor {anchor!r} not found in document")
        return prior.replace(anchor, content, 1)

    raise ValueError(f"unknown operation {operation!r}")


def _replace_section(document: str, anchor: str, new_content: str) -> str:
    """Replace the content of the section headed by ``anchor``.

    Anchor is a heading string like ``"## Tone"`` or just ``"Tone"``. The
    section runs from the heading until the next heading of the same or
    lower (broader) level.
    """
    # Normalize the anchor
    anchor_line = anchor if anchor.startswith("#") else f"## {anchor}"

    lines = document.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == anchor_line.strip():
            start = i
            break
    if start is None:
        raise ValueError(f"anchor section {anchor_line!r} not found")

    # Determine the heading level
    heading_level = len(anchor_line) - len(anchor_line.lstrip("#"))

    # Find the end of the section (next heading of same or lower level)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= heading_level:
                end = j
                break

    # Rebuild: keep pre-section, rewritten section, post-section
    pre = "\n".join(lines[:start])
    post = "\n".join(lines[end:])
    rewritten = f"{anchor_line.strip()}\n\n{new_content.rstrip()}\n"
    parts = []
    if pre:
        parts.append(pre)
    parts.append(rewritten)
    if post:
        parts.append(post)
    return "\n".join(parts) + ("\n" if not parts[-1].endswith("\n") else "")


register_applier("SoulEdit", SoulEditApplier())
