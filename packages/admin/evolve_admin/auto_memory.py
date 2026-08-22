"""evolve_admin.auto_memory — Inventory of OpenClaw's auto-memory.

Tier 2.4 of the OpenClaw admin coverage roadmap
(`docs/roadmap-openclaw-admin-coverage-2026-05-10.md`).

OpenClaw's per-bot auto-memory lives at
``/Users/<bot>/.openclaw/workspace/memory/``. The dir holds the
``MEMORY.md`` index plus dated journal entries
(``YYYY-MM-DD.md``) and topical subdirs (``articles/``, ``.dreams/``,
etc.) that grow passively as the bot operates. A sibling embedding
+ FTS index lives at ``/Users/<bot>/.openclaw/memory/main.sqlite``
— it's a derived artifact, not the source content, but its size
matters for disk accounting.

(Pre-rename Claude Code wrote per-project markdown memory under
``~/.claude/projects/<encoded>/memory/``. OC has moved to the
single-dir-per-bot layout above; the legacy path is no longer
populated and evolve has no ACL there, so we don't scan it.)

Phase A is **inventory only**. No signals, no thresholds, no
content scanning. Phase B (later) will add threshold signals and
integration with ``content_scan`` for sensitive-content surfacing;
Phase C (later) adds optional backup rotation.

Access pattern: ``deploy.set_evolve_read_acl()`` grants the evolve
user inherited read ACL on ``/Users/<bot>/.openclaw/``, which
covers both ``workspace/memory/`` and ``memory/main.sqlite``. No
sudo or /tmp staging needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


# Auto-memory directory (markdown source) and embedding/FTS index
# (derived sqlite) — both under .openclaw/, both covered by the
# evolve-read ACL grant.
_MEMORY_SUBDIR = ".openclaw/workspace/memory"
_INDEX_DB_SUBDIR = ".openclaw/memory/main.sqlite"


@dataclass
class BotMemoryInventory:
    """Auto-memory inventory for one bot."""

    bot_id: str
    bot_user: str  # macOS account name (may differ from bot_id)
    home: str  # /Users/<user>
    memory_dir: str  # /Users/<user>/.openclaw/workspace/memory
    memory_dir_exists: bool
    file_count: int = 0
    total_bytes: int = 0
    oldest_modified_at: str = ""  # ISO8601 UTC; "" if no files
    newest_modified_at: str = ""  # ISO8601 UTC; "" if no files
    index_db_path: str = ""  # /Users/<user>/.openclaw/memory/main.sqlite
    index_db_size_bytes: int = 0  # 0 if missing or unreadable
    index_db_modified_at: str = ""  # ISO8601 UTC; "" if missing
    error: str = ""  # populated if walk failed (e.g. PermissionError)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "bot_user": self.bot_user,
            "home": self.home,
            "memory_dir": self.memory_dir,
            "memory_dir_exists": self.memory_dir_exists,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "oldest_modified_at": self.oldest_modified_at,
            "newest_modified_at": self.newest_modified_at,
            "index_db_path": self.index_db_path,
            "index_db_size_bytes": self.index_db_size_bytes,
            "index_db_modified_at": self.index_db_modified_at,
            "error": self.error,
        }


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _walk_memory_dir(memory_dir: Path) -> tuple[int, int, float, float]:
    """Recursively walk a memory dir, returning (count, bytes, oldest_mtime, newest_mtime).

    Includes subdirectory contents (``articles/``, ``.dreams/``,
    etc.) — OC's FTS index treats those as part of the corpus.
    Hidden subdirs are NOT skipped; the operator should see all
    accumulated content for accurate disk accounting.
    """
    count = 0
    total = 0
    oldest = float("inf")
    newest = 0.0
    for entry in memory_dir.rglob("*"):
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        count += 1
        total += stat.st_size
        if stat.st_mtime < oldest:
            oldest = stat.st_mtime
        if stat.st_mtime > newest:
            newest = stat.st_mtime
    return count, total, oldest, newest


def scan_bot(
    bot_id: str,
    *,
    bot_user: str | None = None,
    home: Path | None = None,
) -> BotMemoryInventory:
    """Scan one bot's auto-memory directory + index DB.

    Pass ``home`` directly in tests; production callers resolve via
    ``evolve_admin.config.bot_home(bot_id)``.
    """
    bot_user = bot_user or bot_id
    home = home or Path(f"/Users/{bot_user}")
    memory_dir = home / _MEMORY_SUBDIR
    index_db = home / _INDEX_DB_SUBDIR

    inv = BotMemoryInventory(
        bot_id=bot_id,
        bot_user=bot_user,
        home=str(home),
        memory_dir=str(memory_dir),
        memory_dir_exists=memory_dir.exists(),
        index_db_path=str(index_db),
    )

    # Index DB (best-effort stat; missing or unreadable → zeros)
    try:
        st = index_db.stat()
        inv.index_db_size_bytes = st.st_size
        inv.index_db_modified_at = _iso_utc(st.st_mtime)
    except OSError:
        pass

    if not memory_dir.exists():
        return inv

    try:
        count, total, oldest, newest = _walk_memory_dir(memory_dir)
    except PermissionError as exc:
        inv.error = f"memory dir not readable: {exc}"
        return inv
    except OSError as exc:
        inv.error = f"memory dir walk failed: {exc}"
        return inv

    inv.file_count = count
    inv.total_bytes = total
    if count:
        inv.oldest_modified_at = _iso_utc(oldest)
        inv.newest_modified_at = _iso_utc(newest)
    return inv


def scan_pod(network: dict[str, Any]) -> dict[str, Any]:
    """Scan every bot in ``network.members``. Returns a summary dict
    suitable for the /api/auto-memory/inventory endpoint."""
    try:
        from evolve_admin.config import bot_home, get_bot_user
    except ImportError:
        bot_home = None
        get_bot_user = None

    members: list[str] = list(network.get("members") or [])
    bots: list[dict[str, Any]] = []
    for bot_id in members:
        user = get_bot_user(bot_id, network) if get_bot_user else bot_id
        home = bot_home(bot_id, network) if bot_home else Path(f"/Users/{user}")
        inv = scan_bot(bot_id, bot_user=user, home=home)
        bots.append(inv.to_dict())

    total_bytes = sum(b["total_bytes"] for b in bots)
    total_files = sum(b["file_count"] for b in bots)
    total_index_bytes = sum(b["index_db_size_bytes"] for b in bots)
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bots": bots,
        "pod_total_bytes": total_bytes,
        "pod_total_files": total_files,
        "pod_index_db_bytes": total_index_bytes,
    }
