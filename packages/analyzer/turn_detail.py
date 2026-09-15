"""turn_detail — Per-turn drilldown for the admin Turn Audit drawer.

Given a ``turn_id``, gathers everything we know about that single turn:

  1. The full ``turn_annotation`` record from
     ``{shared_dir}/annotations/{bot_id}/{date}.jsonl`` (all fields, not just
     the subset the audit table renders).

  2. Matching ``cost_event`` records — joined by ``session_id`` + ``ts``
     proximity (≤ 60s) since cost_event records don't carry ``turn_id``.
     Gives ``trigger_kind`` and ``cache_state`` lineage.

  3. The ``turns-{date}.jsonl`` row from the bot's
     ``.openclaw/workspace/memory/`` (or shared turns dir) — adds
     ``source``, ``channel``, ``auth_mode``.

  4. Best-effort OC session transcript slice. Each pod stores transcripts
     in its own way; we probe a handful of plausible locations under
     ``{bot_home}/.openclaw/`` and return whatever we find. When nothing
     matches, ``transcript`` is ``None`` with a ``transcript_status``
     reason — the drawer degrades gracefully.

All free-form text from the transcript is run through a lightweight
redaction pass that scrubs obvious credential patterns (``sk-...``,
``ghp_...``, long random hex, JWTs) before returning it. Each section
is also bounded at ``MAX_SECTION_BYTES`` and flagged when truncated.

The admin server runs as the ``evolve`` user, which has macOS ACL read
on every bot's ``.openclaw/`` directory — see CLAUDE.md. We use direct
``Path.read_text()`` first and fall back to ``sudo /bin/cat`` for any
file that throws PermissionError.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


# Per-section cap for transcript content. The drawer is read-only and
# meant for quick auditing — multi-megabyte transcript dumps would just
# blow up the response.
MAX_SECTION_BYTES = 32 * 1024

# How wide a window to consider when joining cost_event by ts (since
# cost_event lacks turn_id). 60 seconds is generous — turn_annotation
# and cost_event are typically written within the same second.
COST_EVENT_TS_WINDOW_SECONDS = 60

# How many days back to scan for the turn_id. Annotation files are
# day-keyed; if the user clicks a row from "Last 30d" we may need to
# scan that whole window. The audit table caps lookback at 30, so 35
# days gives us enough headroom for slow clicks at midnight boundaries.
DEFAULT_LOOKBACK_DAYS = 35


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def get_turn_detail(
    shared_dir: Path,
    bot_id: str,
    turn_id: str,
    *,
    bot_home: Path | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """Return the full per-turn detail bundle for ``turn_id``.

    Returns ``{"error": "..."}`` if the annotation isn't found in the
    lookback window. Otherwise returns a dict with keys:

        annotation:        full turn_annotation record (or null)
        turn_record:       turns-{date}.jsonl row (or null)
        cost_events:       list of joined cost_event records (may be [])
        transcript:        {"system": ..., "user": ..., "assistant": ...,
                            "tool_calls": [...], "tools_invoked": int,
                            "tool_summary": {tool: count, ...}} or null
        transcript_status: "ok" | "not_found" | "redacted" | "unreadable"
        transcript_source: filesystem path we read from (when ok)
    """
    annotation, ann_date = _find_annotation(shared_dir, bot_id, turn_id, lookback_days)
    if annotation is None:
        return {"error": f"turn_id {turn_id!r} not found for bot {bot_id!r}"}

    session_id = annotation.get("session_id") or ""
    ann_ts = annotation.get("ts") or ""

    # Join cost_event by session+ts proximity.
    cost_events = _find_cost_events(shared_dir, bot_id, session_id, ann_ts, ann_date)

    # Pull the matching turns-{date}.jsonl row for source/channel.
    turn_record = _find_turn_record(bot_id, session_id, ann_ts, ann_date, bot_home)

    # Best-effort OC transcript read.
    transcript, status, source_path = _find_transcript(
        bot_id, session_id, turn_id, ann_date, bot_home
    )

    return {
        "annotation": annotation,
        "turn_record": turn_record,
        "cost_events": cost_events,
        "transcript": transcript,
        "transcript_status": status,
        "transcript_source": source_path,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Annotation lookup
# ─────────────────────────────────────────────────────────────────────────────


def _find_annotation(
    shared_dir: Path, bot_id: str, turn_id: str, lookback_days: int
) -> tuple[dict | None, str | None]:
    """Search the bot's annotation files for a turn_annotation matching turn_id.

    Returns (record, YYYY-MM-DD-of-file). Walks newest → oldest; most clicks
    target recent rows so this short-circuits fast in practice.
    """
    ann_dir = shared_dir / "annotations" / bot_id
    if not ann_dir.exists():
        return None, None

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    files = sorted(
        (p for p in ann_dir.glob("*.jsonl") if not p.name.startswith("cost_events-")),
        reverse=True,
    )
    for jf in files:
        day_str = jf.stem[:10]
        if day_str < cutoff:
            break
        for rec in _read_jsonl_lines(jf):
            if (
                rec.get("type") == "turn_annotation"
                and rec.get("turn_id") == turn_id
            ):
                return rec, day_str
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# cost_event join
# ─────────────────────────────────────────────────────────────────────────────


def _find_cost_events(
    shared_dir: Path,
    bot_id: str,
    session_id: str,
    ann_ts: str,
    ann_date: str | None,
) -> list[dict]:
    """Return cost_event records matching session_id + close-enough ts.

    cost_event is written by the converter to ``cost_events-{date}.jsonl``
    sibling files; older records may live inside the legacy ``{date}.jsonl``
    with type=cost_event. We check both. Records with no session_id are
    filtered out so we don't accidentally pull every event from the day.
    """
    if not session_id:
        return []

    ann_dt = _parse_ts(ann_ts)
    candidate_dates = _adjacent_dates(ann_date or "")
    ann_dir = shared_dir / "annotations" / bot_id
    if not ann_dir.exists():
        return []

    matches: list[dict] = []
    seen: set[tuple] = set()  # dedup across sources by (ts, cost_usd)
    for d in candidate_dates:
        for path in (
            ann_dir / f"cost_events-{d}.jsonl",
            ann_dir / f"{d}.jsonl",
        ):
            for rec in _read_jsonl_lines(path):
                if rec.get("type") not in (None, "cost_event"):
                    # legacy file mixes record types — skip non-cost rows
                    continue
                if rec.get("session_id") != session_id:
                    continue
                rec_dt = _parse_ts(rec.get("ts") or "")
                if ann_dt is not None and rec_dt is not None:
                    if abs((rec_dt - ann_dt).total_seconds()) > COST_EVENT_TS_WINDOW_SECONDS:
                        continue
                key = (rec.get("ts"), rec.get("cost_usd"))
                if key in seen:
                    continue
                seen.add(key)
                matches.append(rec)
    matches.sort(key=lambda r: r.get("ts") or "")
    return matches


# ─────────────────────────────────────────────────────────────────────────────
# turn-collector record (.openclaw/workspace/memory/turns-{date}.jsonl)
# ─────────────────────────────────────────────────────────────────────────────


def _find_turn_record(
    bot_id: str,
    session_id: str,
    ann_ts: str,
    ann_date: str | None,
    bot_home: Path | None,
) -> dict | None:
    """Return the matching ``turns-{date}.jsonl`` row, if we can find one.

    Falls back across the candidate dirs the rest of the codebase uses
    (shared turns dir → workspace/memory → home-derived workspace/memory).
    """
    if not session_id:
        return None

    candidate_dirs = _turn_record_dirs(bot_id, bot_home)
    ann_dt = _parse_ts(ann_ts)

    for d in _adjacent_dates(ann_date or ""):
        for turns_dir in candidate_dirs:
            path = turns_dir / f"turns-{d}.jsonl"
            for rec in _read_jsonl_lines(path):
                if rec.get("session_id") != session_id:
                    continue
                rec_dt = _parse_ts(rec.get("ts") or "")
                if ann_dt is not None and rec_dt is not None:
                    if abs((rec_dt - ann_dt).total_seconds()) > COST_EVENT_TS_WINDOW_SECONDS:
                        continue
                return rec
    return None


def _turn_record_dirs(bot_id: str, bot_home: Path | None) -> list[Path]:
    """Mirror the candidate-dirs strategy from server.resolve_bot_paths."""
    if bot_home is not None:
        home = bot_home
    else:
        try:
            from evolve_config import bot_home as _bh
            home = _bh(bot_id)
        except (ImportError, Exception):
            import pwd
            try:
                home = Path(pwd.getpwnam(bot_id).pw_dir)
            except KeyError:
                home = Path(f"/Users/{bot_id}")
    return [
        Path(f"/Users/Shared/evolve/{bot_id}/turns"),
        home / ".openclaw" / "workspace" / "memory",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# OC transcript probing
# ─────────────────────────────────────────────────────────────────────────────


def _find_transcript(
    bot_id: str,
    session_id: str,
    turn_id: str,
    ann_date: str | None,
    bot_home: Path | None,
) -> tuple[dict | None, str, str | None]:
    """Best-effort OC transcript read.

    Returns (transcript_dict, status, source_path).

    Different OpenClaw versions / deployments arrange session transcripts
    slightly differently. Rather than coupling tightly to one layout, we
    probe a handful of plausible locations and accept the first one that
    yields readable JSON/JSONL. None match → status="not_found".
    """
    if not session_id:
        return None, "no_session_id", None

    if bot_home is not None:
        home = bot_home
    else:
        try:
            from evolve_config import bot_home as _bh
            home = _bh(bot_id)
        except (ImportError, Exception):
            import pwd
            try:
                home = Path(pwd.getpwnam(bot_id).pw_dir)
            except KeyError:
                home = Path(f"/Users/{bot_id}")

    # Candidate transcript paths, in priority order. These cover the
    # common OC layouts; if a deployment uses a different shape the
    # drawer just shows annotation + cost_events (still useful).
    probes: list[Path] = [
        home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / session_id / "transcript.jsonl",
        home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / f"{session_id}.jsonl",
        home / ".openclaw" / "sessions" / session_id / "transcript.jsonl",
        home / ".openclaw" / "sessions" / f"{session_id}.jsonl",
        home / ".openclaw" / "workspace" / "sessions" / session_id / "transcript.jsonl",
    ]

    for path in probes:
        text = _read_file_text(path)
        if text is None:
            continue
        parsed = _parse_transcript(text, turn_id)
        if parsed is not None:
            return parsed, "ok", str(path)

    return None, "not_found", None


def _parse_transcript(text: str, turn_id: str) -> dict | None:
    """Reduce a raw transcript file to a per-turn slice.

    Tries JSONL first (most likely shape); on failure tries a single JSON
    document. The transcript is expected to contain message records with
    ``role`` (system/user/assistant) and ``content`` plus tool_use /
    tool_result blocks. We pick out the slice tied to ``turn_id`` (when
    we can find it) and otherwise fall back to "last user + last
    assistant" as a useful approximation.

    The output dict matches what the drawer renders:
        {
          system:    {chars, text, truncated},
          user:      {chars, text, truncated},
          assistant: {chars, text, truncated},
          tool_calls: [{tool, input_chars, result_chars, truncated}],
          tools_invoked: int,
          tool_summary: {tool: count, ...},
        }
    """
    records: list[dict] = []
    # Try JSONL first
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            records = []  # not JSONL after all
            break
        if isinstance(rec, dict):
            records.append(rec)

    # Try whole-document JSON if JSONL gave nothing usable
    if not records:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(doc, dict) and "messages" in doc and isinstance(doc["messages"], list):
            records = [m for m in doc["messages"] if isinstance(m, dict)]
        elif isinstance(doc, list):
            records = [m for m in doc if isinstance(m, dict)]
        else:
            return None

    if not records:
        return None

    # Slice to the messages tied to this turn_id when annotated; fall back
    # to "last user + last assistant" pair otherwise. Different writers tag
    # turns differently (turn_id, turnId, turn) — accept any of them.
    matched = [
        r for r in records
        if any(
            r.get(k) == turn_id
            for k in ("turn_id", "turnId", "turn")
        )
    ]
    if not matched:
        matched = _last_user_assistant_pair(records)

    return _summarize_messages(records, matched)


def _last_user_assistant_pair(records: list[dict]) -> list[dict]:
    """Approximate a single-turn slice from a flat conversation list."""
    last_user = None
    last_assistant = None
    for rec in reversed(records):
        role = rec.get("role")
        if role == "assistant" and last_assistant is None:
            last_assistant = rec
        elif role == "user" and last_user is None:
            last_user = rec
        if last_user is not None and last_assistant is not None:
            break
    out = []
    if last_user is not None:
        out.append(last_user)
    if last_assistant is not None:
        out.append(last_assistant)
    return out


def _summarize_messages(all_records: list[dict], turn_records: list[dict]) -> dict:
    """Collapse a list of message records into the drawer payload."""
    system_text = ""
    for rec in all_records:
        if rec.get("role") == "system":
            system_text = _content_to_text(rec.get("content"))
            if system_text:
                break

    user_text = ""
    assistant_text = ""
    tool_calls: list[dict] = []
    tool_summary: dict[str, int] = {}

    for rec in turn_records:
        role = rec.get("role")
        content = rec.get("content")
        if role == "user":
            user_text = _content_to_text(content) or user_text
        elif role == "assistant":
            assistant_text = _content_to_text(content) or assistant_text
        # Tool blocks may live inline in assistant content (Anthropic-style)
        # or as separate tool_use / tool_result records. Handle both.
        for tu, tr in _extract_tool_pairs(rec, content):
            tool_name = tu.get("name") or tu.get("tool") or "unknown"
            tool_summary[tool_name] = tool_summary.get(tool_name, 0) + 1
            tu_input = json.dumps(tu.get("input", {}), ensure_ascii=False) if tu.get("input") is not None else ""
            tr_text = _content_to_text(tr.get("content")) if tr else ""
            tu_input_red = _redact(tu_input)
            tr_text_red = _redact(tr_text)
            tool_calls.append({
                "tool": tool_name,
                "input_chars": len(tu_input),
                "result_chars": len(tr_text),
                "input": _truncate(tu_input_red),
                "input_truncated": len(tu_input.encode("utf-8", "replace")) > MAX_SECTION_BYTES,
                "result": _truncate(tr_text_red),
                "result_truncated": len(tr_text.encode("utf-8", "replace")) > MAX_SECTION_BYTES,
            })

    return {
        "system": _section(system_text),
        "user": _section(user_text),
        "assistant": _section(assistant_text),
        "tool_calls": tool_calls,
        "tools_invoked": sum(tool_summary.values()),
        "tool_summary": tool_summary,
    }


def _extract_tool_pairs(rec: dict, content: Any) -> Iterator[tuple[dict, dict | None]]:
    """Yield (tool_use_block, tool_result_block_or_none) pairs from a record.

    Anthropic-style content arrays look like:
        [{"type": "tool_use", "id": "...", "name": "...", "input": {...}},
         {"type": "tool_result", "tool_use_id": "...", "content": "..."}]
    """
    if not isinstance(content, list):
        return
    use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    result_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    by_id = {b.get("tool_use_id"): b for b in result_blocks if b.get("tool_use_id")}
    for tu in use_blocks:
        tr = by_id.get(tu.get("id"))
        yield tu, tr


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` field to a plain-text string.

    Accepts string content, list-of-blocks (Anthropic shape), or arbitrary
    dicts. Tool blocks are skipped here — they're emitted via
    _extract_tool_pairs instead so we don't double-count.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype in (None, "input_text", "output_text"):
                    parts.append(str(block.get("text", "") or block.get("content", "")))
                # tool_use / tool_result handled separately
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content)


def _section(text: str) -> dict:
    """Wrap a section's text with chars + truncated metadata + redaction."""
    redacted = _redact(text)
    raw_bytes = len(text.encode("utf-8", "replace"))
    return {
        "chars": len(text),
        "text": _truncate(redacted),
        "truncated": raw_bytes > MAX_SECTION_BYTES,
    }


def _truncate(text: str) -> str:
    """Cap a string at MAX_SECTION_BYTES (UTF-8). Adds an ellipsis marker."""
    if not text:
        return ""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= MAX_SECTION_BYTES:
        return text
    cut = encoded[:MAX_SECTION_BYTES].decode("utf-8", "ignore")
    return cut + "\n…[truncated]…"


# ─────────────────────────────────────────────────────────────────────────────
# Redaction
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that look credential-shaped. We're conservative: better to scrub
# a real-looking string than to leak a key in an admin page screenshot. The
# patterns are loose by design — false positives just turn into "[redacted]"
# in the drawer.
_REDACTION_PATTERNS = [
    # Anthropic / OpenAI / GitHub keys
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[redacted-key]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "[redacted-key]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[redacted-key]"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "[redacted-key]"),
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "[redacted-key]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[redacted-key]"),
    # Slack / Discord / Telegram
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "[redacted-key]"),
    (re.compile(r"\b[0-9]{9,11}:AA[A-Za-z0-9_\-]{20,}\b"), "[redacted-key]"),  # Telegram
    # Generic Bearer/Authorization
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"), "Bearer [redacted-key]"),
    (re.compile(r"(?i)authorization:\s*[A-Za-z0-9_\-\.]{20,}"), "Authorization: [redacted-key]"),
    # JWT (three b64url segments separated by dots)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b"), "[redacted-jwt]"),
    # Long random hex (>= 32 chars) — common for tokens
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "[redacted-hex]"),
    # Common key= / token= / password= forms
    (re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{8,}['\"]?"),
     r"\1=[redacted]"),
]


def _redact(text: str) -> str:
    """Run a defensive scrub over free-form text."""
    if not text:
        return ""
    out = text
    for pat, repl in _REDACTION_PATTERNS:
        out = pat.sub(repl, out)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _adjacent_dates(day_str: str) -> list[str]:
    """Return [day-1, day, day+1] as ISO dates, falling back to today.

    Annotation files are bucketed by the record's own ts (UTC slice), so
    a turn near midnight UTC may live in either the previous or next day's
    file. Checking ±1 day is cheap insurance.
    """
    try:
        d = date.fromisoformat(day_str)
    except (TypeError, ValueError):
        d = date.today()
    return [(d - timedelta(days=1)).isoformat(), d.isoformat(), (d + timedelta(days=1)).isoformat()]


def _parse_ts(raw: str) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_file_text(path: Path) -> str | None:
    """Read a file as text. Direct first; sudo /bin/cat fallback for ACL gaps.

    The admin server runs as evolve and has macOS ACL read on every bot's
    .openclaw/ via set_evolve_read_acl. Direct reads cover ACL'd bots; the
    sudo fallback covers bots that haven't been redeployed since the ACL
    rollout (or non-bot files like Shared/evolve/).
    """
    try:
        return path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None
    except OSError:
        return None


def _read_jsonl_lines(path: Path) -> Iterator[dict]:
    """Yield JSONL records from path; tolerant of missing / unreadable files."""
    text = _read_file_text(path)
    if text is None:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec
