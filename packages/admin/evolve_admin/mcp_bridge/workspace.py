"""
evolve_admin.mcp_bridge.workspace — Sandboxed file I/O for bot workspaces.

Bot home directories are mode 700, so all operations run as the bot's
macOS user (resolved via config.get_bot_user — may differ from bot_id)
via `sudo -u {bot_user}`. The workspace root is the resolved path from
the bot's openclaw.json (via evolve_admin.config.get_bot_workspace).

All paths are validated against the workspace root to prevent traversal.
File metadata (`stat`) is read via a platform-keyed format flag — BSD
`stat -f "%z %m"` on macOS, GNU `stat -c "%s %Y"` on Linux — because the
two `stat(1)` implementations share neither the flag nor the format
language (`-f` means `--file-system` on GNU, which made the macOS form a
permanent no-op on Linux pods; same class of bug as PR #3259's audit.py).
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import date
from pathlib import Path

from platform_profile import get_profile

from ..config import get_bot_user, load_network


class WorkspaceError(Exception):
    pass


class BotWorkspace:
    def __init__(self, bot_id: str, root: Path) -> None:
        self.bot_id = bot_id
        self.root = root
        self.bot_user = get_bot_user(bot_id, load_network())

    # ── Path safety ──────────────────────────────────────────────────────────

    def _safe_path(self, rel_path: str) -> Path:
        """Resolve rel_path against workspace root; reject any traversal."""
        # Normalise: strip leading slash so joiners like /memory/foo.md work
        rel_path = rel_path.lstrip("/")
        resolved = (self.root / rel_path).resolve()
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            raise WorkspaceError(f"Path traversal rejected: {rel_path!r}")
        return resolved

    # ── Low-level subprocess helpers (sync, run in thread pool by callers) ───

    def _run(self, cmd: list[str], input_text: str | None = None) -> str:
        """Run a command as the bot user. Returns stdout. Raises WorkspaceError on failure."""
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=15,
            cwd="/tmp",  # neutral cwd that any user can access
        )
        if result.returncode != 0:
            raise WorkspaceError(result.stderr.strip() or f"Command failed: {cmd}")
        return result.stdout

    def _sudo(self, *args: str, input_text: str | None = None) -> str:
        return self._run(["sudo", "-n", "-u", self.bot_user,*args], input_text=input_text)

    # ── Async public API (all block via asyncio.to_thread) ───────────────────

    async def read(self, rel_path: str) -> str:
        path = self._safe_path(rel_path)
        try:
            return await asyncio.to_thread(self._sudo, "cat", str(path))
        except WorkspaceError as e:
            raise WorkspaceError(f"Cannot read {rel_path}: {e}") from e

    async def write(self, rel_path: str, content: str, overwrite: bool = False) -> None:
        path = self._safe_path(rel_path)
        await asyncio.to_thread(
            self._sudo, "mkdir", "-p", str(path.parent)
        )
        if not overwrite:
            exists_result = subprocess.run(
                ["sudo", "-n", "-u", self.bot_user,"test", "-f", str(path)],
                capture_output=True, timeout=5, cwd="/tmp",
            )
            if exists_result.returncode == 0:
                raise WorkspaceError(
                    f"File already exists: {rel_path}. Pass overwrite=true to replace."
                )
        # Write via tee (handles binary-safe write as the bot user)
        try:
            await asyncio.to_thread(
                self._sudo, "tee", str(path), input_text=content
            )
        except WorkspaceError as e:
            raise WorkspaceError(f"Cannot write {rel_path}: {e}") from e

    async def append(self, rel_path: str, content: str) -> None:
        """Append content to a file; create it if it does not exist."""
        path = self._safe_path(rel_path)
        await asyncio.to_thread(
            self._sudo, "mkdir", "-p", str(path.parent)
        )
        try:
            await asyncio.to_thread(
                self._sudo, "tee", "-a", str(path), input_text=content
            )
        except WorkspaceError as e:
            raise WorkspaceError(f"Cannot append to {rel_path}: {e}") from e

    async def list_dir(self, rel_path: str = ".") -> list[str]:
        path = self._safe_path(rel_path)
        try:
            output = await asyncio.to_thread(self._sudo, "ls", "-1", str(path))
            return [f for f in output.splitlines() if f.strip()]
        except WorkspaceError:
            return []

    async def exists(self, rel_path: str) -> bool:
        path = self._safe_path(rel_path)
        result = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "-u", self.bot_user,"test", "-e", str(path)],
            capture_output=True, timeout=5, cwd="/tmp",
        )
        return result.returncode == 0

    async def stat(self, rel_path: str) -> dict:
        """Return size_bytes and last_modified (ISO timestamp) for a file.

        The file is bot-owned (home is mode 0700), so this shells out as the
        bot user via sudo rather than calling os.stat directly. The stat
        format flag is platform-keyed: BSD `stat -f "%z %m"` on macOS, GNU
        `stat -c "%s %Y"` on Linux — both yield "size mtime_epoch". The old
        hardcoded BSD form failed on every Linux pod (`-f` is --file-system
        there) and the `except WorkspaceError: pass` below silently returned
        {} on each call (mirror of PR #3259's audit.py fix).
        """
        path = self._safe_path(rel_path)
        # BSD %z=size %m=mtime_epoch ; GNU %s=size %Y=mtime_epoch
        if get_profile().name == "linux":
            stat_flag, stat_fmt = "-c", "%s %Y"
        else:
            stat_flag, stat_fmt = "-f", "%z %m"
        try:
            out = await asyncio.to_thread(
                self._sudo, "stat", stat_flag, stat_fmt, str(path)
            )
            parts = out.strip().split()
            if len(parts) >= 2:
                import time
                mtime = int(parts[1])
                return {
                    "size_bytes": int(parts[0]),
                    "last_modified": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)
                    ),
                }
        except WorkspaceError:
            pass
        return {}

    # ── Convenience paths ────────────────────────────────────────────────────

    def today_note_path(self) -> str:
        return f"memory/{date.today().isoformat()}.md"

    def memory_path(self) -> str:
        return "memory/MEMORY.md"

    def tasks_path(self) -> str:
        return "memory/tasks.md"

    def handoffs_dir(self) -> str:
        return "handoffs"
