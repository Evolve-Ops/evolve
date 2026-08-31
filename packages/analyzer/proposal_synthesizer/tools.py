"""proposal_synthesizer.tools — Read-only investigation tools.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §5.3.

Each tool is a callable the LLM can invoke during a synthesis run.
All tools are read-only — no writes, no network beyond what Anthropic
itself does. Tools are scoped to operational data the synthesizer
needs to reason about candidates:

  - signal history     → ``read_signal_history``
  - cost ledger        → ``read_cost_ledger``
  - session transcript → ``read_session_transcript``
  - bot config         → ``read_bot_config``
  - workspace file     → ``read_workspace_file``
  - watchdog log       → ``read_watchdog_log``
  - audit findings     → ``read_audit_findings``
  - proposal history   → ``read_proposal_history``
  - git log            → ``git_log``
  - git blame          → ``git_blame``

Each tool has a JSON Schema for its arguments and a free-form
docstring for the model. The :data:`TOOL_REGISTRY` exposes both the
schema (for the Anthropic tools parameter) and the executable (for
dispatch).

**Failure mode.** Tools never raise into the synthesizer loop. On
any error they return ``{"error": "<reason>"}`` so the LLM can
react. Hard caps (size, count, days) live in the tool code, not in
the schema, so a model that tries to ask for "10000 sessions" can't
DoS the synthesizer.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from evolve_config import bot_home as _bot_home
from platform_profile import get_profile


log = logging.getLogger(__name__)


# Hard size cap on any single tool's response (text + structured data).
# Keeps the model from accidentally consuming huge files into context.
MAX_TOOL_RESPONSE_BYTES = 64 * 1024
MAX_TRANSCRIPT_TURNS = 50
MAX_LEDGER_EVENTS = 200
MAX_GIT_LOG_COMMITS = 30


# ─────────────────────────────────────────────────────────────────────────────
# Tool descriptor
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Tool:
    """One investigation tool the synthesizer can call."""

    name: str
    description: str
    input_schema: dict  # JSON Schema for the LLM
    fn: Callable[[dict, Path], dict]  # (args, shared_dir) -> structured result

    def call(self, args: dict, shared_dir: Path) -> dict:
        """Execute the tool, catching any exception as a structured error."""
        try:
            result = self.fn(args or {}, shared_dir)
        except Exception as exc:  # noqa: BLE001 — never raise into the loop
            log.warning("tool %r raised: %s", self.name, exc, exc_info=True)
            return {"error": f"{type(exc).__name__}: {exc}"}
        return _enforce_size_cap(result)


def _enforce_size_cap(result: dict) -> dict:
    """If a tool returned more than MAX_TOOL_RESPONSE_BYTES of JSON,
    truncate with a clear marker so the model can react instead of
    silently losing data."""
    encoded = json.dumps(result)
    if len(encoded) <= MAX_TOOL_RESPONSE_BYTES:
        return result
    truncated = encoded[: MAX_TOOL_RESPONSE_BYTES - 200]
    return {
        "truncated": True,
        "truncated_at_bytes": MAX_TOOL_RESPONSE_BYTES,
        "preview": truncated,
        "hint": "Response exceeded the per-tool cap; ask for a narrower slice.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# File-read primitives — match CLAUDE.md's access pattern
# ─────────────────────────────────────────────────────────────────────────────


def _read_text_with_sudo_fallback(path: Path) -> str | None:
    """Try direct read; fall back to ``sudo /bin/cat`` per CLAUDE.md.

    The evolve user has ACL read on ``.openclaw/`` for bots deployed
    through the new path. The fallback handles bots not yet
    redeployed.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError):
        pass
    except OSError:
        return None
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return r.stdout
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — read_signal_history
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_signal_history(args: dict, shared_dir: Path) -> dict:
    fingerprint = str(args.get("fingerprint") or "").strip()
    window_days = int(args.get("window_days") or 30)
    if not fingerprint:
        return {"error": "fingerprint is required"}
    try:
        from signals import store as signals_store
    except ImportError:
        return {"error": "signals.store unavailable"}

    matches: list[dict] = []
    for sd in ("firing", "snoozed", "archived"):
        for sig in signals_store.iter_signals(shared_dir, subdirs=(sd,)):
            if sig.signature != fingerprint:
                continue
            matches.append(
                {
                    "id": sig.id,
                    "producer": sig.producer,
                    "type": sig.type,
                    "bot_id": sig.bot_id or "",
                    "state": sig.state,
                    "severity": sig.severity,
                    "title": sig.title,
                    "created_at": sig.created_at,
                    "last_observed_at": sig.last_observed_at,
                    "observation_count": sig.observation_count,
                }
            )
    matches.sort(key=lambda m: m.get("last_observed_at", ""), reverse=True)
    return {"signal_history": matches[:50], "total": len(matches)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — read_cost_ledger
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_cost_ledger(args: dict, shared_dir: Path) -> dict:
    bot_id = str(args.get("bot_id") or "").strip()
    days = int(args.get("days") or 7)
    trigger_kind = args.get("trigger_kind")  # optional filter
    if not bot_id:
        return {"error": "bot_id is required"}
    days = max(1, min(30, days))

    try:
        import cost_ledger
    except ImportError:
        return {"error": "cost_ledger module unavailable"}

    events = list(cost_ledger.read_events(bot_id, days=days, shared_dir=shared_dir))
    if trigger_kind:
        events = [e for e in events if e.get("trigger_kind") == trigger_kind]

    total_cost = sum(float(e.get("cost_usd") or 0.0) for e in events)
    by_kind: dict[str, float] = {}
    for e in events:
        k = str(e.get("trigger_kind") or "unknown")
        by_kind[k] = by_kind.get(k, 0.0) + float(e.get("cost_usd") or 0.0)

    # Return a summary plus the most recent N events. Avoid dumping
    # the entire ledger into the model's context.
    events_recent = sorted(events, key=lambda e: e.get("ts", ""), reverse=True)
    return {
        "bot_id": bot_id,
        "window_days": days,
        "filter_trigger_kind": trigger_kind,
        "event_count": len(events),
        "total_cost_usd": round(total_cost, 6),
        "cost_by_trigger_kind": {
            k: round(v, 6) for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])
        },
        "recent_events": events_recent[:MAX_LEDGER_EVENTS],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — read_session_transcript
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_session_transcript(args: dict, shared_dir: Path) -> dict:
    bot_id = str(args.get("bot_id") or "").strip()
    session_id = str(args.get("session_id") or "").strip()
    if not bot_id or not session_id:
        return {"error": "bot_id and session_id are required"}

    sess_root = _bot_home(bot_id) / ".openclaw" / "sessions" / session_id
    transcript_path = sess_root / "transcript.jsonl"

    text = _read_text_with_sudo_fallback(transcript_path)
    if text is None:
        return {"error": f"transcript not readable: {transcript_path}"}

    turns: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return {
        "bot_id": bot_id,
        "session_id": session_id,
        "turn_count": len(turns),
        "first_turns": turns[:5],
        "last_turns": turns[-MAX_TRANSCRIPT_TURNS:],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — read_bot_config
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_bot_config(args: dict, shared_dir: Path) -> dict:
    bot_id = str(args.get("bot_id") or "").strip()
    if not bot_id:
        return {"error": "bot_id is required"}
    path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    text = _read_text_with_sudo_fallback(path)
    if text is None:
        return {"error": f"openclaw.json not readable for {bot_id}"}
    try:
        return {"bot_id": bot_id, "config": json.loads(text)}
    except json.JSONDecodeError as e:
        return {"error": f"openclaw.json parse failure: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — read_workspace_file
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_workspace_file(args: dict, shared_dir: Path) -> dict:
    bot_id = str(args.get("bot_id") or "").strip()
    rel_path = str(args.get("path") or "").strip()
    if not bot_id or not rel_path:
        return {"error": "bot_id and path are required"}
    # Defense-in-depth: refuse ../ traversal even though our subprocess
    # path joining would normalize it.
    if ".." in Path(rel_path).parts or rel_path.startswith("/"):
        return {"error": "path must be workspace-relative; no '..' or absolute paths"}
    full = _bot_home(bot_id) / ".openclaw" / "workspace" / rel_path
    text = _read_text_with_sudo_fallback(full)
    if text is None:
        return {"error": f"file not readable: {full}"}
    return {
        "bot_id": bot_id,
        "path": rel_path,
        "size_bytes": len(text.encode("utf-8")),
        "content": text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 6 — read_watchdog_log
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_watchdog_log(args: dict, shared_dir: Path) -> dict:
    days = int(args.get("days") or 7)
    days = max(1, min(30, days))
    event_type = args.get("event_type")  # optional filter

    log_dir = shared_dir / "watchdog"
    if not log_dir.exists():
        return {"events": [], "total": 0, "note": "watchdog log absent"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict] = []
    for f in sorted(log_dir.glob("*.jsonl")):
        try:
            day_str = f.stem  # YYYY-MM-DD
            day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if day < cutoff:
                continue
        except ValueError:
            continue
        try:
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and rec.get("type") != event_type:
                    continue
                events.append(rec)
        except OSError:
            continue

    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return {
        "window_days": days,
        "filter_event_type": event_type,
        "total": len(events),
        "events": events[:100],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool 7 — read_audit_findings
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_audit_findings(args: dict, shared_dir: Path) -> dict:
    findings_path = shared_dir / "audit" / "current-findings.json"
    if not findings_path.exists():
        return {"findings": [], "note": "no current-findings.json"}
    try:
        raw = findings_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"audit findings unreadable: {e}"}
    return {"findings": data}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 8 — read_proposal_history
# ─────────────────────────────────────────────────────────────────────────────


def _tool_read_proposal_history(args: dict, shared_dir: Path) -> dict:
    bot_id = args.get("bot_id")
    generator_id = args.get("generator_id")
    days = int(args.get("days") or 30)

    try:
        from arbiter import store as proposal_store
    except ImportError:
        return {"error": "arbiter.store unavailable"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    matches: list[dict] = []
    for sd in ("pending", "snoozed", "applied", "archived"):
        try:
            for p in proposal_store.iter_proposals(shared_dir, subdirs=(sd,)):
                if bot_id and p.bot_id != bot_id:
                    continue
                if generator_id and p.generator_id != generator_id:
                    continue
                try:
                    created = datetime.fromisoformat(p.created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                except ValueError:
                    pass
                matches.append(
                    {
                        "id": p.id,
                        "generator_id": p.generator_id,
                        "bot_id": p.bot_id,
                        "status": p.status,
                        "headline": p.admin_surface_summary,
                        "created_at": p.created_at,
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    matches.sort(key=lambda m: m["created_at"], reverse=True)
    return {"window_days": days, "total": len(matches), "proposals": matches[:50]}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 9 — git_log
# ─────────────────────────────────────────────────────────────────────────────


# The deploy checkout is where the synthesizer can read history. Per
# CLAUDE.md, /Users/Shared/evolve-repo is the canonical deploy location on
# macOS (/var/lib/evolve/repo on Linux — platform-keyed). Configurable for
# tests (they set this module attribute directly).
DEPLOY_REPO_PATH = Path(get_profile().deploy_checkout_default)


def _tool_git_log(args: dict, shared_dir: Path) -> dict:
    path = args.get("path")
    days = int(args.get("days") or 30)
    days = max(1, min(180, days))

    repo = DEPLOY_REPO_PATH
    if not repo.exists():
        # Tests can set DEPLOY_REPO_PATH at module level; or pass repo
        # in args (undocumented to the LLM).
        override = args.get("_repo_path_for_test")
        if override:
            repo = Path(override)
        if not repo.exists():
            return {"error": f"repo not found at {repo}"}

    cmd = [
        "git",
        "log",
        f"--since={days}.days.ago",
        f"--max-count={MAX_GIT_LOG_COMMITS}",
        "--pretty=format:%H%x09%an%x09%ad%x09%s",
        "--date=iso-strict",
    ]
    if path:
        cmd.append("--")
        cmd.append(path)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(repo), timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"git log failed: {e}"}
    if r.returncode != 0:
        return {"error": f"git log nonzero: {r.stderr.strip()[:200]}"}

    commits: list[dict] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        commits.append(
            {
                "sha": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            }
        )
    return {"path": path, "days": days, "commits": commits, "total": len(commits)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 10 — git_blame
# ─────────────────────────────────────────────────────────────────────────────


def _tool_git_blame(args: dict, shared_dir: Path) -> dict:
    path = str(args.get("path") or "").strip()
    line_start = int(args.get("line_start") or 1)
    line_end = int(args.get("line_end") or line_start)
    if not path:
        return {"error": "path is required"}
    line_start = max(1, line_start)
    line_end = max(line_start, line_end)
    if line_end - line_start > 200:
        return {"error": "line range too wide; cap is 200 lines"}

    repo = DEPLOY_REPO_PATH
    if not repo.exists():
        override = args.get("_repo_path_for_test")
        if override:
            repo = Path(override)
        if not repo.exists():
            return {"error": f"repo not found at {repo}"}

    cmd = [
        "git",
        "blame",
        "-L",
        f"{line_start},{line_end}",
        "--date=iso-strict",
        "--",
        path,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(repo), timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"git blame failed: {e}"}
    if r.returncode != 0:
        return {"error": f"git blame nonzero: {r.stderr.strip()[:200]}"}
    return {"path": path, "lines": r.stdout.splitlines()[:200]}


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry — the LLM sees the schemas; the agent dispatches via the fns
# ─────────────────────────────────────────────────────────────────────────────


TOOL_REGISTRY: dict[str, Tool] = {
    "read_signal_history": Tool(
        name="read_signal_history",
        description=(
            "Return prior Signal records matching a fingerprint. Use this "
            "to understand how often the underlying condition has fired "
            "and whether it has resolved on its own."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fingerprint": {
                    "type": "string",
                    "description": "Signal signature (often matches CandidateProposal.fingerprint).",
                },
                "window_days": {
                    "type": "integer",
                    "default": 30,
                    "description": "Look back this many days. Max 30.",
                },
            },
            "required": ["fingerprint"],
        },
        fn=_tool_read_signal_history,
    ),
    "read_cost_ledger": Tool(
        name="read_cost_ledger",
        description=(
            "Read cost_event records for a bot. Returns a summary "
            "(total cost, breakdown by trigger_kind) plus recent events. "
            "Optional trigger_kind filter narrows to one kind."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "days": {"type": "integer", "default": 7, "description": "Max 30."},
                "trigger_kind": {
                    "type": "string",
                    "description": "Optional filter: heartbeat, user_turn, classifier, summarizer, etc.",
                },
            },
            "required": ["bot_id"],
        },
        fn=_tool_read_cost_ledger,
    ),
    "read_session_transcript": Tool(
        name="read_session_transcript",
        description=(
            "Read a session's turn-by-turn transcript. Returns the first "
            "5 turns and the last 50 turns — enough to spot stuck loops, "
            "retry storms, and runaway subagents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["bot_id", "session_id"],
        },
        fn=_tool_read_session_transcript,
    ),
    "read_bot_config": Tool(
        name="read_bot_config",
        description=(
            "Read the bot's openclaw.json. Use this to check heartbeat "
            "cadence, model overrides, cron definitions, and integration "
            "config."
        ),
        input_schema={
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
        },
        fn=_tool_read_bot_config,
    ),
    "read_workspace_file": Tool(
        name="read_workspace_file",
        description=(
            "Read a workspace file on a bot. Path is workspace-relative; "
            "absolute paths and '..' traversal are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path, e.g. 'heartbeats.md'.",
                },
            },
            "required": ["bot_id", "path"],
        },
        fn=_tool_read_workspace_file,
    ),
    "read_watchdog_log": Tool(
        name="read_watchdog_log",
        description=(
            "Read watchdog event records (pod-wide) from the trailing "
            "window. Optional event_type filter narrows results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "description": "Max 30."},
                "event_type": {"type": "string"},
            },
        },
        fn=_tool_read_watchdog_log,
    ),
    "read_audit_findings": Tool(
        name="read_audit_findings",
        description="Read the pod's current audit findings (security warden, etc.).",
        input_schema={"type": "object", "properties": {}},
        fn=_tool_read_audit_findings,
    ),
    "read_proposal_history": Tool(
        name="read_proposal_history",
        description=(
            "List recent Proposals matching a bot and/or generator. Use "
            "this to check whether a similar proposal was recently "
            "approved, rejected, or applied."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bot_id": {"type": "string"},
                "generator_id": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
        },
        fn=_tool_read_proposal_history,
    ),
    "git_log": Tool(
        name="git_log",
        description=(
            "Return recent commits from the deploy checkout. Optional "
            "path argument limits to commits touching that path. Use this "
            "to find when behavior changed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional repo-relative path."},
                "days": {"type": "integer", "default": 30, "description": "Max 180."},
            },
        },
        fn=_tool_git_log,
    ),
    "git_blame": Tool(
        name="git_blame",
        description=(
            "Run git blame on a line range. Use this to find who last "
            "touched a specific block of code and why."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
            },
            "required": ["path", "line_start"],
        },
        fn=_tool_git_blame,
    ),
}


def anthropic_tools_schema() -> list[dict]:
    """Return the tool definitions in the shape Anthropic's API expects."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in TOOL_REGISTRY.values()
    ]


def dispatch_tool(tool_name: str, args: dict, shared_dir: Path) -> dict:
    """Look up and execute a tool by name. Returns the tool's result
    (or an ``{"error": ...}`` dict if the tool is unknown)."""
    tool = TOOL_REGISTRY.get(tool_name)
    if tool is None:
        return {"error": f"unknown tool: {tool_name!r}"}
    return tool.call(args, shared_dir)
