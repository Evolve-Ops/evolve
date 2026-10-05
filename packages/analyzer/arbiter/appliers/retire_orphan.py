"""arbiter.appliers.retire_orphan — Retire a workspace orphan file.

Used by app_posture_reflection's ``delete_orphan`` proposals (PR9).

The applier is deliberately *not* a deleter:
  1. Reads the orphan content from the bot's workspace (evolve has ACL
     read on /Users/<bot>/.openclaw/ via deploy.set_evolve_read_acl).
  2. Copies the content to {shared_dir}/app_posture/<bot>/orphan_archive/
     <YYYY-MM-DD>-<basename> (evolve owns shared_dir).
  3. Appends the path to {shared_dir}/app_posture/<bot>/
     orphan_exclusions.json so future weekly posture reviews skip it.

The file in the workspace is **not unlinked** — evolve has no delete
grant on bot workspaces, and physical removal would require new
infrastructure (per-bot launchd helper or new sudoers grant) that's
out of scope here. Retire-and-exclude is the safe, reversible analog:
the orphan stops appearing in reflection, and there's a content
snapshot operators can recover from. The bot can clean up the actual
file at its own pace if it wants.

Reversibility:
  - Snapshot captures the prior exclusions list.
  - Revert removes the path from exclusions (the archive copy stays
    intact — cheap to leave around and useful for audit).
  - The file in the workspace is untouched in either direction, so no
    data-loss risk on this applier.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json

from arbiter.appliers.base import (
    ApplyResult,
    RevertResult,
    register_applier,
)
from schema.proposal import RetireOrphan


# Resolved at apply time so tests can swap shared_dir without re-importing.
_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    """Override the shared_dir used by the applier (tests + alternate pods)."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def _bot_workspace(bot_id: str) -> Path:
    """Workspace path for a bot. Resolves the macOS account name via
    evolve_config.bot_home so that bots with a 'user' override in
    network.json (e.g. team_bot_b/personal_bot_user) are handled correctly. If
    openclaw.json has remapped the workspace, the read will fail with a
    clear error rather than silently reading the wrong path."""
    try:
        from evolve_config import bot_home as _bh
        return _bh(bot_id) / ".openclaw" / "workspace"
    except ImportError:
        import pwd
        try:
            return Path(pwd.getpwnam(bot_id).pw_dir) / ".openclaw" / "workspace"
        except KeyError:
            return Path(f"/Users/{bot_id}/.openclaw/workspace")


def _archive_dir(bot_id: str) -> Path:
    return _SHARED_DIR / "app_posture" / bot_id / "orphan_archive"


def _exclusions_path(bot_id: str) -> Path:
    return _SHARED_DIR / "app_posture" / bot_id / "orphan_exclusions.json"


def _safe_basename(path: str) -> str:
    """Sanitize a workspace-relative path into a filesystem-safe filename
    for the archive. Replaces slashes with double underscores and strips
    anything that isn't a portable filename character. The archive
    timestamp prefix gives uniqueness even when paths collide."""
    rel = path.strip().lstrip("./").replace("/", "__")
    rel = re.sub(r"[^A-Za-z0-9._-]", "_", rel)
    return rel or "orphan"


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_exclusions(bot_id: str) -> list[str]:
    """Read the bot's orphan_exclusions.json. Returns a sorted unique
    list of path strings; missing or malformed file → empty list."""
    p = _exclusions_path(bot_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return sorted({str(x) for x in data if isinstance(x, str) and x.strip()})
    if isinstance(data, dict):
        # Accept {"paths": [...]} envelope as a forward-compat hedge for
        # adding metadata fields later (retire_at, retired_by, etc.).
        paths = data.get("paths") or []
        return sorted({str(x) for x in paths if isinstance(x, str) and x.strip()})
    return []


def _write_exclusions(bot_id: str, paths: list[str]) -> None:
    """Write the bot's orphan_exclusions.json. Always writes the bare
    list shape — the dict envelope is read-supported for forward-compat
    but not the canonical write shape."""
    deduped = sorted(set(paths))
    path = _exclusions_path(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, deduped, sort_keys=True)


# Public read API — used by app_posture_review.py to filter orphan walks.
def load_exclusions(bot_id: str, *, shared_dir: Path | None = None) -> set[str]:
    """Load the bot's orphan exclusions as a set. Module entry point for
    callers outside the applier (the posture review reads this on every
    cycle to skip retired orphans)."""
    if shared_dir is not None:
        prev = _SHARED_DIR
        try:
            set_shared_dir(shared_dir)
            return set(_read_exclusions(bot_id))
        finally:
            set_shared_dir(prev)
    return set(_read_exclusions(bot_id))


# ─────────────────────────────────────────────────────────────────────────────
# Applier
# ─────────────────────────────────────────────────────────────────────────────


class RetireOrphanApplier:
    """Apply a RetireOrphan: archive the file's content + exclude the
    path from future posture reviews."""

    def capture_snapshot(self, action: RetireOrphan, bot_id: str) -> dict:
        """Snapshot the prior exclusions list. The workspace file itself
        isn't touched by apply, so we don't need to snapshot its content
        for revert — though we DO archive its content as the user-facing
        safety net."""
        prior = _read_exclusions(action.bot_id)
        return {
            "action_kind": "RetireOrphan",
            "bot_id": action.bot_id,
            "path": action.path,
            "prior_exclusions": prior,
            "exclusions_path": str(_exclusions_path(action.bot_id)),
        }

    def apply(self, action: RetireOrphan, bot_id: str) -> ApplyResult:
        # The workspace file lives at {workspace}/{path}. action.path is
        # workspace-relative (per the schema docstring); we resolve here.
        workspace = _bot_workspace(action.bot_id)
        target_file = workspace / action.path

        # Defense against malicious paths (path-traversal, absolute paths,
        # symlinks pointing outside the workspace). The applier may run
        # against an LLM-generated path; the LLM passed through the
        # confidence + inventory-grounding gates upstream, but a final
        # check here is cheap.
        try:
            resolved = target_file.resolve()
            workspace_resolved = workspace.resolve()
            resolved.relative_to(workspace_resolved)
        except (ValueError, OSError) as e:
            return ApplyResult(
                ok=False,
                details={"target_path": str(target_file), "error": str(e)},
                message=(
                    f"refusing to retire {action.path!r}: path resolves "
                    f"outside {workspace}"
                ),
            )

        # ── 1. Archive the file content (best effort — proceed even if
        # the read fails, since the exclusion still has value). ───────────
        archived_path: Path | None = None
        archive_ok = False
        archive_error: str | None = None
        if target_file.exists() and target_file.is_file():
            try:
                _archive_dir(action.bot_id).mkdir(parents=True, exist_ok=True)
                archive_name = f"{_today_iso()}-{_safe_basename(action.path)}"
                archived_path = _archive_dir(action.bot_id) / archive_name
                # Use shutil.copy2 to preserve timestamps; copy not move
                # because we don't have delete privileges on source.
                shutil.copy2(str(target_file), str(archived_path))
                archive_ok = True
            except (OSError, PermissionError) as e:
                archive_error = str(e)
        elif not target_file.exists():
            archive_error = "file does not exist in workspace"
        else:
            archive_error = "target is not a regular file"

        # ── 2. Append to the exclusions list (the load-bearing step —
        # this is what makes the orphan stop appearing in future
        # reflections). ───────────────────────────────────────────────
        try:
            existing = _read_exclusions(action.bot_id)
            if action.path in existing:
                # Already retired in a prior cycle. Idempotent: success
                # but flag it.
                return ApplyResult(
                    ok=True,
                    details={
                        "exclusions_path": str(_exclusions_path(action.bot_id)),
                        "already_retired": True,
                        "archive_ok": archive_ok,
                        "archived_path": str(archived_path) if archived_path else None,
                    },
                    message=(
                        f"retire_orphan no-op: {action.path!r} already in "
                        f"{action.bot_id}'s exclusions list"
                    ),
                )
            new_list = existing + [action.path]
            _write_exclusions(action.bot_id, new_list)
        except OSError as e:
            return ApplyResult(
                ok=False,
                details={"error": str(e)},
                message=f"failed to write exclusions list: {e}",
            )

        details = {
            "exclusions_path": str(_exclusions_path(action.bot_id)),
            "archived_path": str(archived_path) if archived_path else None,
            "archive_ok": archive_ok,
            "archive_error": archive_error,
        }
        if archive_ok:
            message = (
                f"retired {action.bot_id}/{action.path}: archived to "
                f"{archived_path} and added to exclusions"
            )
        else:
            # The exclusion still happened — that's the user-visible
            # behavior they approved. Surface the archive miss in the
            # message so the operator knows the safety snapshot didn't
            # land (rare: would happen if the file was already gone or
            # ACL read failed).
            message = (
                f"retired {action.bot_id}/{action.path}: added to exclusions "
                f"(archive skipped: {archive_error})"
            )
        return ApplyResult(ok=True, details=details, message=message)

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        """Restore the prior exclusions list. The archive copy is left
        in place — operators can recover from it manually if they need
        to, and re-running revert cleanly produces the same end state.

        The workspace file was never touched, so there's nothing to
        restore there."""
        bot = snapshot.get("bot_id") or bot_id
        prior = snapshot.get("prior_exclusions")
        if not isinstance(prior, list):
            return RevertResult(
                ok=False,
                message="snapshot missing or malformed prior_exclusions",
            )
        try:
            _write_exclusions(bot, list(prior))
        except OSError as e:
            return RevertResult(
                ok=False,
                details={"error": str(e)},
                message=f"failed to write exclusions on revert: {e}",
            )
        return RevertResult(
            ok=True,
            details={"exclusions_path": str(_exclusions_path(bot))},
            message=(
                f"reverted retire_orphan: restored exclusions list "
                f"({len(prior)} entries) for {bot}"
            ),
        )


register_applier("RetireOrphan", RetireOrphanApplier())
