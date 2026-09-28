"""
evolve_admin.mcp_bridge.audit — JSONL audit logger for MCP bridge operations.

Every tool call is appended to mcp-audit.jsonl. Write operations log full input;
read operations log only the tool name and target bot (not content, which may be large).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("evolve.mcp_bridge.audit")


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # W10-G #4: audit logging is best-effort — it must NEVER prevent the
        # bridge from binding :5051. A mkdir that hits EACCES / a missing
        # parent (e.g. a log dir the daemon can't create on a fresh pod) used
        # to raise straight out of __init__, taking the whole server down
        # before uvicorn started → systemd restart-loop, nothing bound. Degrade
        # to disabled instead; the warning lands on the StandardError path.
        self._enabled = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._enabled = False
            _log.warning(
                "Audit log disabled — cannot create %s (%s). The bridge will "
                "serve without an audit trail.", path.parent, e)

    def log(
        self,
        *,
        session_id: str,
        client_ip: str,
        tool: str,
        bot: str | None,
        input_summary: str,
        result: str,          # "ok" | "error: <msg>"
        duration_ms: int,
        is_write: bool = False,
    ) -> None:
        entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "client_ip": client_ip,
            "tool": tool,
            "bot": bot,
            "input_summary": input_summary,
            "result": result,
            "duration_ms": duration_ms,
            "write": is_write,
        }
        if not self._enabled:
            return
        line = json.dumps(entry) + "\n"
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as e:
            # Best-effort: a transient write failure must not break a tool call.
            _log.warning("Audit write failed (%s); dropping one entry.", e)

    def tail(self, n: int = 100, writes_only: bool = False) -> list[dict]:
        """Return the last n audit entries, optionally filtered to writes only."""
        if not self._path.exists():
            return []
        try:
            with self._lock:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            entries = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if writes_only and not entry.get("write"):
                    continue
                entries.append(entry)
                if len(entries) >= n:
                    break
            return list(reversed(entries))
        except Exception:
            return []
