"""exec_outcome_watchdog — Surface the bot-intent ↔ execution-outcome gap.

Spec: docs/spec-exec-outcome-watchdog-2026-05-28.md.

Reads ``{shared_dir}/annotations/<bot>/<date>.jsonl`` (turn_annotation
records produced by the OC plugin's TurnObserver/StruggleDetector) and
emits Signals when the bot's exec requests are failing in patterns the
operator should see. Mirrors the cost_watchdog producer shape so the
runner + signal store + suppression machinery work identically.

Four detector families — Phase 1 ships only `tool_error_burst`
(annotation-only, cheapest); Phases 2-4 layer on content inspection.

Failure semantics: every reader fails open. Missing annotation file
returns no signal rather than raising. The producer is best-effort —
silent on missing data, loud on real patterns.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import (
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "exec_outcome_watchdog"

# Signal types this producer emits that get suppressed by an active
# breaker. Mirrors cost_watchdog's pattern. Mapped to "automation"
# because exec-outcome failures fall in the same operator-handle-it
# basket as cron/heartbeat noise — the right response is operator
# attention, not piling on more signals.
_SUPPRESSIBLE_TYPES_TO_CATEGORY: dict[str, str] = {
    "tool_error_burst":   "automation",
    "exec_denied":        "automation",
    "approval_timeout":   "automation",
    "preflight_block":    "automation",
}


DEFAULTS: dict[str, Any] = {
    # tool_error_burst: fires when the trailing-window total of
    # struggle_features.tool_error_count exceeds the threshold across
    # `window_days` of annotation data. Per-bot tunable via
    # exec_outcome_watchdog.bots.<bot>.tool_error_burst_min_count.
    "tool_error_burst_window_days":     7,
    "tool_error_burst_min_count":       5,
    "tool_error_burst_min_sessions":    2,
    "tool_error_burst_max_per_run":     1,
    # exec_denied: same window as tool_error_burst. Fires when at
    # least N denials are seen in the window. Detection requires
    # session-detail content inspection; absent the OC session
    # extractor wiring, this detector silently returns [].
    "exec_denied_window_days":          7,
    "exec_denied_min_count":            1,
    "exec_denied_max_per_run":          5,
    # approval_timeout: same window. Detection requires content matching
    # the OC "approval timed out" / "approval pending" signature; absent
    # session detail, returns []. Fires on first observed timeout —
    # the silent-fail mode is too consequential to require a baseline.
    "approval_timeout_window_days":     7,
    "approval_timeout_min_count":       1,
    "approval_timeout_max_per_run":     5,
    # preflight_block: same window. Detection requires content matching
    # OC v5.26+ preflight signatures (python / node / pipes / && / > / -c).
    # References openclaw#87371.
    "preflight_block_window_days":      7,
    "preflight_block_min_count":        1,
    "preflight_block_max_per_run":      5,
}


def _thresholds_for_bot(bot_id: str, config: dict[str, Any]) -> dict[str, Any]:
    cw_cfg = config.get("exec_outcome_watchdog") or {}
    out: dict[str, Any] = dict(DEFAULTS)
    out.update(cw_cfg.get("defaults") or {})
    out.update((cw_cfg.get("bots") or {}).get(bot_id) or {})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Annotation reader
# ─────────────────────────────────────────────────────────────────────────────


def read_turn_annotations(
    shared_dir: Path,
    bot_id: str,
    *,
    days: int,
    today: date | None = None,
) -> list[dict]:
    """Read ``turn_annotation`` records for ``bot_id`` over the trailing window.

    Files live at ``{shared_dir}/annotations/<bot>/<YYYY-MM-DD>.jsonl``,
    one per day. Missing days are silently skipped. Lines that fail to
    parse are skipped — the file may be mid-write when we read it.

    Returns a flat list (oldest first by file, then by line order
    inside the file). Caller bucket-and-aggregates as needed.
    """
    ann_dir = Path(shared_dir) / "annotations" / bot_id
    if today is None:
        today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for i in range(days):
        d = today - timedelta(days=i)
        path = ann_dir / f"{d.isoformat()}.jsonl"
        try:
            text = path.read_text()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") != "turn_annotation":
                continue
            out.append(rec)
    # Sort ascending by ts so callers iterate oldest-first.
    out.sort(key=lambda r: str(r.get("ts") or ""))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Mode 1: tool_error_burst
# ─────────────────────────────────────────────────────────────────────────────


def _struggle_count(record: dict, feature: str) -> int:
    """Pull a single struggle_features count from a turn_annotation record.

    Falls back to ``struggle_raw`` when ``struggle_features`` is absent
    (different schema_version shapes both populate the same fields).
    Defaults to 0 — a missing field means no struggle was recorded.
    """
    for key in ("struggle_features", "struggle_raw"):
        feat = record.get(key)
        if isinstance(feat, dict):
            try:
                return int(feat.get(feature, 0) or 0)
            except (TypeError, ValueError):
                continue
    return 0


def detect_tool_error_burst(
    bot_id: str,
    annotations: list[dict],
    *,
    window_days: int,
    min_count: int,
    min_sessions: int,
    max_per_run: int,
) -> list[dict]:
    """Fire when trailing-window tool_error_count crosses threshold.

    Triggers when:
      - total ``struggle_features.tool_error_count`` over ``window_days`` ≥ min_count
      - at least ``min_sessions`` distinct sessions contributed (don't
        fire on a single session having a bad day — that's session_token_outlier's
        job)

    Severity escalates at 2× threshold. One Signal per bot.
    """
    if not annotations:
        return []
    total = 0
    sessions_with_errors: set[str] = set()
    tool_retry_total = 0
    restart_marker_total = 0
    by_session_errors: dict[str, int] = {}
    for rec in annotations:
        errors = _struggle_count(rec, "tool_error_count")
        if errors <= 0:
            continue
        total += errors
        tool_retry_total += _struggle_count(rec, "tool_retry_count")
        restart_marker_total += _struggle_count(rec, "restart_markers")
        sid = str(rec.get("session_id") or "")
        if sid:
            sessions_with_errors.add(sid)
            by_session_errors[sid] = by_session_errors.get(sid, 0) + errors

    if total < min_count:
        return []
    if len(sessions_with_errors) < min_sessions:
        return []

    # Worst-offender session — useful for the operator next-action step.
    worst_session = ""
    worst_count = 0
    for sid, n in by_session_errors.items():
        if n > worst_count:
            worst_count = n
            worst_session = sid

    severity = "alert" if total >= 2 * min_count else "warn"
    return [
        {
            "signature": make_signature(PRODUCER, "tool_error_burst", bot_id),
            "producer": PRODUCER,
            "type": "tool_error_burst",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {total} tool errors over {window_days}d "
                f"across {len(sessions_with_errors)} sessions"
            ),
            "body": (
                f"{bot_id} had {total} tool errors over the last {window_days} "
                f"days across {len(sessions_with_errors)} sessions "
                f"(retries: {tool_retry_total}, restart markers: "
                f"{restart_marker_total}). The bot is attempting actions that "
                f"the system is blocking — usually exec policy, approval "
                f"timeout, or MCP unreachable. The exec_outcome_investigator "
                f"will attempt to attribute the cause."
            ),
            "details": {
                "bot_id": bot_id,
                "window_days": window_days,
                "tool_error_total": total,
                "tool_retry_total": tool_retry_total,
                "restart_markers_total": restart_marker_total,
                "sessions_with_errors": len(sessions_with_errors),
                "worst_session_id": worst_session,
                "worst_session_errors": worst_count,
                "threshold_count": min_count,
                "vector": "exec_outcome",
                "magnitude": 2 if total >= 2 * min_count else 1,
                "what_it_means": (
                    f"`{bot_id}` ran into {total} tool errors over the recent "
                    f"window across {len(sessions_with_errors)} distinct "
                    "sessions. Tool errors are the bot trying to do "
                    "something the system blocks — exec policy denial, "
                    "approval timeouts, MCP not reachable. Repeated "
                    "errors burn LLM tokens (the bot retries or rephrases) "
                    "and the user sees confusion in the chat surface. "
                    "Most common shape: the bot's manifest declares a "
                    "capability that the exec allowlist hasn't been "
                    "updated to permit."
                ),
                "fix_steps": (
                    f"1. Open Sessions filtered to `{bot_id}` and inspect "
                    f"session `{worst_session[:8] if worst_session else '—'}` "
                    "for the failing tool calls\n"
                    "2. The exec_outcome_investigator proposal carries the "
                    "attribution; check the Alerts page for `cause_key`\n"
                    "3. For exec denials: review and extend "
                    f"`/Users/{bot_id}/.openclaw/exec-approvals.json` to "
                    "permit the blocked command\n"
                    "4. For approval timeouts: enable a faster approval "
                    "channel for this bot (Telegram DM, admin UI push) so "
                    "you see requests inside the 30-min OC TTL\n"
                    "5. If errors are intentional (bot is asking for "
                    "things it shouldn't), raise the threshold via "
                    f"`exec_outcome_watchdog.bots.{bot_id}."
                    "tool_error_burst_min_count`"
                ),
            },
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Modes 2-4: content-inspection detectors
#
# These read OC session tool_result content via turn_detail.extract_tool_pairs.
# When session detail isn't available (no extractor wired, session
# database missing), the detectors silently return [] — the
# tool_error_burst alone still fires from Mode 1.
# ─────────────────────────────────────────────────────────────────────────────


# Denial signatures observed in OC tool_result content. Stays case-insensitive.
# Conservative — these are phrases OC reliably emits when blocking; false
# positives are worse than misses (the operator can always inspect manually).
# Patterns use unrestricted-suffix matching so we don't brittle on OC's
# phrasing across versions (block/blocked/blocking all match).
_DENIAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bexec[- ]?policy\b.*?\b(?:deny|denied|block|refus)", re.I),
    re.compile(r"\bnot\s+approved\b", re.I),
    re.compile(r"\bcommand\s+not\s+(?:allowed|permitted|approved)\b", re.I),
    re.compile(r"\b(?:exec|run)\s+denied\b", re.I),
    re.compile(r"\bdenied\b.*?\bexec", re.I),
)

# Approval-timeout signatures. The canonical OC string is "approval timed out";
# also catch "approval expired" and the bot's own narration of the failure.
_APPROVAL_TIMEOUT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bapproval\s+timed\s+out\b", re.I),
    re.compile(r"\bapproval\s+expired\b", re.I),
    re.compile(r"\bcommand\s+did\s+not\s+run\b", re.I),
    re.compile(r"\bapproval[- ]?timeout\b", re.I),
)

# Preflight-block signatures — OC v5.26+ blocks specific shell shapes even
# with exec=full. References openclaw#87371. Patterns use unrestricted-suffix
# matching (e.g. `block` matches "block", "blocked", "blocking") so we don't
# brittle on OC's exact phrasing across versions.
_PREFLIGHT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bpreflight\b.*?\b(?:block|deny|refuse)", re.I),
    re.compile(r"\b(?:python|node)\b.*?\bblock", re.I),
    re.compile(r"\bcomplex\s+(?:syntax|expression)\b.*?\bblock", re.I),
    re.compile(r"\bshell\s+(?:redirect|pipe)\b.*?\b(?:block|den)", re.I),
)


# OC's "allowlist-miss" subscript indicates that the root cause is a
# policy gap, not a routing problem — even when the surface phrasing
# mentions "approval-timeout". Verified live on the mini 2026-05-28:
# OC emits "Exec denied (gateway id=..., approval-timeout (allowlist-miss)): cmd"
# for these cases, and the bot's own interpretation is "exec denied due to
# an allowlist miss." Classifying these as `timeout` would suggest the
# wrong operator fix (change notification channel) instead of the right
# one (extend the allowlist).
_ALLOWLIST_MISS_PATTERN = re.compile(
    r"\ballowlist[- ]miss\b|\(allowlist\)", re.I,
)


def _classify_tool_result(content: str) -> str | None:
    """Return one of 'denied', 'timeout', 'preflight', or None.

    Precedence:
      1. Allowlist-miss subscript (OC-specific): the surface may say
         "approval-timeout" but the root cause is allowlist gap → denied.
      2. Approval timeout (most distinctive when standalone).
      3. Preflight block (specific phrasing).
      4. Generic denial.

    None means the content didn't match any known failure signature — the
    tool may have failed for a different reason (network, runtime), which
    falls under the umbrella tool_error_burst Signal without a specific
    mode classification.
    """
    if not content:
        return None
    # OC's explicit cause flag wins over the surface phrasing.
    if _ALLOWLIST_MISS_PATTERN.search(content):
        return "denied"
    for pat in _APPROVAL_TIMEOUT_PATTERNS:
        if pat.search(content):
            return "timeout"
    for pat in _PREFLIGHT_PATTERNS:
        if pat.search(content):
            return "preflight"
    for pat in _DENIAL_PATTERNS:
        if pat.search(content):
            return "denied"
    return None


# Parse OC's async-failure message: "Exec denied (gateway id=..., approval-timeout
# (allowlist-miss)): <cmd>". OC nests parens inside the parenthesized cause
# group, so we use a non-greedy match looking for "):" terminator (the closing
# paren of the cause group, followed by the command's colon). Verified against
# live security_bot session data 2026-05-28.
_OC_ASYNC_FAILURE_CMD = re.compile(
    # Optional `.*?\)` matches OC's nested-parens cause group when present
    # (e.g. "denied (gateway id=..., approval-timeout (allowlist-miss))"),
    # and is skipped for the simple form "denied: cmd". Non-greedy + the
    # required `:` terminator lets the regex backtrack across nested
    # parens to find the outer closing paren.
    r"Exec\s+(?:denied|blocked)\b(?:.*?\))?:\s*(?P<cmd>.+?)(?:\n|$)",
    re.I | re.DOTALL,
)


def _extract_command_tool_name(text: str) -> str:
    """Best-effort tool_name extraction from an OC failure message.

    OC's failure surface is plain text ("Exec denied (...): ps aux | grep ...").
    We take the command's first whitespace-separated token as the tool_name
    so grouping is meaningful — `ps`, `python3`, `kubectl` etc.
    """
    m = _OC_ASYNC_FAILURE_CMD.search(text)
    if not m:
        return "exec"
    cmd = m.group("cmd").strip()
    if not cmd:
        return "exec"
    first = cmd.split(None, 1)[0]
    return first or "exec"


def _unwrap_session_record(rec: dict) -> tuple[str | None, str, list]:
    """Normalize a session record to (turn_id, role, content_blocks).

    Handles two shapes:
      * OC's wrapped format: ``{"type": "message", "id": "...",
        "message": {"role": ..., "content": [...]}}`` — verified live
        on the mini 2026-05-28.
      * Anthropic-flat format used by test fixtures and some other OC
        versions: ``{"id": "...", "role": ..., "content": [...]}``.

    Returns ``(turn_id, role, content)``. ``role`` is ``""`` when the
    record isn't a message; ``content`` is ``[]`` when malformed.
    """
    turn_id = str(rec.get("id") or "")
    if rec.get("type") == "message":
        msg = rec.get("message") or {}
        role = msg.get("role") or ""
        content = msg.get("content")
    elif "role" in rec:
        role = rec.get("role") or ""
        content = rec.get("content")
    else:
        return turn_id, "", []
    if not isinstance(content, list):
        content = []
    return turn_id, role, content


def _classify_oc_session_messages(
    session_id: str, records: list[dict]
) -> list[dict]:
    """Walk session message records, emit outcome dicts for classified failures.

    Handles three failure shapes:
      * Anthropic tool_result blocks in user messages (test fixtures
        + any OC version that uses structured tool_result blocks).
      * Assistant-role records with inline tool_result blocks (the
        shape some legacy fixtures use — `tool_use` and `tool_result`
        in the same content array).
      * OC's async-failure text shape: user-role text content starting
        "[timestamp] An async command..." with "Exec denied (...): cmd".
        Verified live on the mini 2026-05-28 against security_bot session
        031ff8ba-d021-4108-8548-2c7749684f81.

    Each output dict is the same shape ``detect_exec_denied`` consumes.
    """
    out: list[dict] = []
    last_tool_call_name: str | None = None
    for rec in records:
        turn_id, role, content = _unwrap_session_record(rec)
        if not content:
            continue
        # Build a tool_use_id → name map for THIS record so inline
        # tool_use + tool_result pairs (assistant-role fixtures) get
        # the right attribution.
        local_tool_names: dict[str, str] = {}
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("toolCall", "tool_use"):
                name = str(block.get("name") or block.get("tool") or "exec")
                bid = str(block.get("id") or "")
                if bid:
                    local_tool_names[bid] = name
                last_tool_call_name = name

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            # Path A: Anthropic tool_result blocks (any role).
            if block_type == "tool_result":
                tr_content = block.get("content")
                if isinstance(tr_content, list):
                    text = " ".join(
                        b.get("text", "") for b in tr_content
                        if isinstance(b, dict) and isinstance(b.get("text"), str)
                    )
                elif isinstance(tr_content, str):
                    text = tr_content
                else:
                    text = ""
                classification = _classify_tool_result(text)
                if classification is None:
                    continue
                tu_id = str(block.get("tool_use_id") or "")
                tool_name = local_tool_names.get(tu_id) \
                    or last_tool_call_name or "exec"
                out.append({
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "tool_input_preview": "",
                    "tool_result_preview": text[:300],
                    "classification": classification,
                })
                last_tool_call_name = None
                continue
            # Path B: OC's text-content async-failure surface (user-role).
            if block_type == "text" and role == "user":
                text = block.get("text") or ""
                classification = _classify_tool_result(text)
                if classification is None:
                    continue
                tool_name = (
                    _extract_command_tool_name(text)
                    if "Exec" in text
                    else (last_tool_call_name or "exec")
                )
                out.append({
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "tool_name": tool_name,
                    "tool_input_preview": "",
                    "tool_result_preview": text[:300],
                    "classification": classification,
                })
                last_tool_call_name = None
    return out


def _default_session_loader(
    bot_id: str, session_id: str, *, bot_home: Path | None = None,
) -> list[dict] | None:
    """Read OC's session JSONL for ``(bot_id, session_id)`` from disk.

    Production path verified live 2026-05-28: OC writes session
    transcripts to
    ``/Users/<bot>/.openclaw/agents/main/sessions/<session_id>.jsonl``,
    one record per line. We use the standard sudo-fallback pattern
    (evolve user has ACL read on .openclaw/) and return raw JSON
    records; ``_classify_oc_session_messages`` does the parsing.

    Returns ``None`` on read failure — the caller treats that as "no
    session detail available" and silently skips this session.
    """
    if bot_home is None:
        try:
            from evolve_config import bot_home as _bh
            bot_home = _bh(bot_id)
        except Exception:
            return None
    path = (
        bot_home / ".openclaw" / "agents" / "main" / "sessions"
        / f"{session_id}.jsonl"
    )
    text: str | None = None
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        text = None
    if text is None:
        # sudo /bin/cat fallback — mirrors cost_watchdog._read_with_sudo_fallback
        import subprocess
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                text = r.stdout
        except (subprocess.TimeoutExpired, OSError):
            return None
    if not text:
        return None
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _iter_failed_tool_outcomes(
    session_loader: Any,
    bot_id: str,
    annotations: list[dict],
) -> list[dict]:
    """Walk annotations, load session detail, classify each failed tool call.

    Each output element is a dict with:
      - session_id, turn_id
      - tool_name
      - tool_input_preview (truncated, may be empty for text-shape failures)
      - tool_result_preview (truncated)
      - classification: 'denied' | 'timeout' | 'preflight'

    ``session_loader`` is a callable ``(bot_id, session_id)`` returning the
    raw OC session records (or None). When None, falls back to
    ``_default_session_loader``. Tests inject a fake.
    """
    if session_loader is None:
        return []
    out: list[dict] = []
    seen_sessions: set[str] = set()
    for rec in annotations:
        if _struggle_count(rec, "tool_error_count") <= 0:
            continue
        sid = str(rec.get("session_id") or "")
        if not sid or sid in seen_sessions:
            continue
        seen_sessions.add(sid)
        try:
            session_records = session_loader(bot_id, sid)
        except Exception:
            continue
        if not session_records:
            continue
        out.extend(_classify_oc_session_messages(sid, session_records))
    return out


def detect_exec_denied(
    bot_id: str,
    outcomes: list[dict],
    *,
    window_days: int,
    min_count: int,
    max_per_run: int,
) -> list[dict]:
    """One Signal per (bot, blocked tool) when denial count crosses threshold.

    ``outcomes`` is the output of ``_iter_failed_tool_outcomes`` —
    pre-classified. The detector slices to ``classification == 'denied'``
    and groups by tool_name so the operator sees which specific
    commands are blocked.
    """
    denied = [o for o in outcomes if o["classification"] == "denied"]
    if not denied:
        return []
    by_tool: dict[str, list[dict]] = {}
    for o in denied:
        by_tool.setdefault(o["tool_name"], []).append(o)
    candidates = [
        (tool, occs) for tool, occs in by_tool.items() if len(occs) >= min_count
    ]
    candidates.sort(key=lambda kv: len(kv[1]), reverse=True)

    out: list[dict] = []
    for tool, occs in candidates[:max_per_run]:
        count = len(occs)
        latest = occs[-1]
        scope_key = f"{bot_id}/{tool}"
        severity = "alert" if count >= 3 * min_count else "warn"
        out.append({
            "signature": make_signature(PRODUCER, "exec_denied", scope_key),
            "producer": PRODUCER,
            "type": "exec_denied",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: tool `{tool}` denied {count}× over {window_days}d"
            ),
            "body": (
                f"{bot_id} attempted `{tool}` {count} times over the last "
                f"{window_days} days; each attempt was denied by the exec "
                f"policy. The most recent denial message: "
                f'"{latest["tool_result_preview"][:120]}…". Usually the bot\'s '
                f"manifest declares this capability but the allowlist hasn't "
                f"been updated — investigator will check."
            ),
            "details": {
                "bot_id": bot_id,
                "tool_name": tool,
                "denial_count": count,
                "window_days": window_days,
                "sample_session_id": latest["session_id"],
                "sample_result_preview": latest["tool_result_preview"],
                "sample_input_preview": latest["tool_input_preview"],
                "vector": "exec_outcome",
                "magnitude": 2 if count >= 3 * min_count else 1,
                "what_it_means": (
                    f"`{bot_id}` repeatedly tried to run `{tool}` and OC "
                    "denied each attempt. Two common causes: (1) the bot's "
                    "manifest declares the capability but the exec-approvals "
                    "allowlist wasn't updated, so OC's policy says no; "
                    "(2) the operator intentionally restricted exec and "
                    "the bot doesn't know yet. The investigator generator "
                    "cross-references the manifest to distinguish."
                ),
                "fix_steps": (
                    f"1. Open Alerts and find the `exec_outcome_investigator` "
                    f"Proposal for `{bot_id}` — `cause_key` names which path\n"
                    "2. For `exec_denied_allowlist_gap` (manifest declares "
                    "the capability), accept the one-click ConfigPatch that "
                    "extends the allowlist\n"
                    "3. For intentional restriction, dismiss the Proposal "
                    "and the bot will stop retrying once it sees enough denials\n"
                    f"4. Manual inspection: ssh pod_admin_user@mini sudo "
                    f"/bin/cat /Users/{bot_id}/.openclaw/exec-approvals.json"
                ),
            },
        })
    return out


def detect_approval_timeout(
    bot_id: str,
    outcomes: list[dict],
    *,
    window_days: int,
    min_count: int,
    max_per_run: int,
) -> list[dict]:
    """One Signal per bot when approval-timeout count crosses threshold.

    Unlike exec_denied (per-tool), approval timeouts are a
    routing/visibility problem at the bot scope — the operator wasn't
    watching, regardless of which tool. So we aggregate at the bot
    level.
    """
    timeouts = [o for o in outcomes if o["classification"] == "timeout"]
    if len(timeouts) < min_count:
        return []
    # Take the most recent N for evidence
    latest = timeouts[-1]
    # Per-tool counts so the Phase 4 attribution rule can detect single-
    # tool recurrence (one tool timing out N times → allowlist gap, same
    # fix as exec_denied) vs. scattered timeouts across many tools (the
    # true operator-routing problem).
    per_tool_counts: dict[str, int] = {}
    for o in timeouts:
        tn = o.get("tool_name") or "exec"
        per_tool_counts[tn] = per_tool_counts.get(tn, 0) + 1
    distinct_tools = sorted(per_tool_counts.keys())
    count = len(timeouts)
    max_single_tool_count = max(per_tool_counts.values()) if per_tool_counts else 0
    top_tool = max(per_tool_counts.items(), key=lambda kv: kv[1])[0] \
        if per_tool_counts else ""
    severity = "alert" if count >= 3 * min_count else "warn"
    return [
        {
            "signature": make_signature(PRODUCER, "approval_timeout", bot_id),
            "producer": PRODUCER,
            "type": "approval_timeout",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {count} exec approval(s) timed out over {window_days}d"
            ),
            "body": (
                f"{bot_id} requested {count} exec approval(s) that timed out "
                f"over the last {window_days} days (tools affected: "
                f"{', '.join(distinct_tools[:5])}). OC's approval TTL is 30 "
                f"minutes — if the operator doesn't see the request inside "
                f"that window, the command silently fails and the bot's "
                f"workflow stalls. exec_outcome_investigator will attribute "
                f"between a routing problem (scattered tools) and a recurring "
                f"allowlist gap (same tool repeatedly)."
            ),
            "details": {
                "bot_id": bot_id,
                "timeout_count": count,
                "distinct_tools": distinct_tools,
                "per_tool_counts": dict(per_tool_counts),
                "max_single_tool_count": max_single_tool_count,
                "top_tool": top_tool,
                "window_days": window_days,
                "sample_session_id": latest["session_id"],
                "sample_result_preview": latest["tool_result_preview"],
                "vector": "exec_outcome",
                "magnitude": 2 if count >= 3 * min_count else 1,
                "what_it_means": (
                    f"`{bot_id}` is asking the operator to approve exec "
                    "actions and the approvals are timing out — OC's 30-min "
                    "TTL elapsed before anyone saw the request. The bot's "
                    "workflow silently stalls and the user-visible artifact "
                    "is often a confused chat reply (the bot already "
                    "composed an optimistic message before knowing the "
                    "exec failed). The investigator decides between two fixes: "
                    "if one tool dominates the timeouts, the right answer is "
                    "to add it to the allowlist (same shape as exec_denied); "
                    "if timeouts are scattered across tools, the operator's "
                    "notification channel is the bottleneck."
                ),
                "fix_steps": (
                    "1. Open the exec_outcome_investigator Proposal for "
                    f"`{bot_id}` — Phase 4 distinguishes recurring-tool "
                    "vs scattered-tools attribution\n"
                    "2. For the recurring-tool case, accept the proposal's "
                    "one-click UpdateExecApproval to add the tool to the "
                    "allowlist; the approval prompt then isn't needed\n"
                    "3. For the scattered-tools case, enable a faster "
                    "approval channel for this bot (admin UI push, evo DM) "
                    "so requests arrive inside OC's 30-min TTL"
                ),
            },
        }
    ]


def detect_preflight_block(
    bot_id: str,
    outcomes: list[dict],
    *,
    window_days: int,
    min_count: int,
    max_per_run: int,
) -> list[dict]:
    """One Signal per bot when OC v5.26+ preflight is blocking commands.

    Distinct from exec_denied because the operator's fix is different:
    preflight blocks come from OC's *parser*, not the allowlist —
    extending allowlist won't help. The bot needs to rephrase (script
    wrapper) or the operator needs to upgrade OC.
    """
    blocks = [o for o in outcomes if o["classification"] == "preflight"]
    if len(blocks) < min_count:
        return []
    count = len(blocks)
    latest = blocks[-1]
    distinct_tools = sorted({o["tool_name"] for o in blocks})
    severity = "warn"
    return [
        {
            "signature": make_signature(PRODUCER, "preflight_block", bot_id),
            "producer": PRODUCER,
            "type": "preflight_block",
            "flavor": "maintenance",
            "severity": severity,
            "scope": "bot",
            "bot_id": bot_id,
            "title": (
                f"{bot_id}: {count} OC preflight block(s) over {window_days}d"
            ),
            "body": (
                f"{bot_id} ran {count} command(s) that OC's preflight blocked "
                f"over the last {window_days} days. Preflight blocks come "
                f"from OC's command parser (v5.26+), not the exec allowlist — "
                f"common triggers are `python`/`node` invocations or shell "
                f"redirects (pipes, `&&`, `>`, `-c`). See openclaw#87371."
            ),
            "details": {
                "bot_id": bot_id,
                "block_count": count,
                "distinct_tools": distinct_tools,
                "window_days": window_days,
                "sample_session_id": latest["session_id"],
                "sample_result_preview": latest["tool_result_preview"],
                "sample_input_preview": latest["tool_input_preview"],
                "vector": "exec_outcome",
                "magnitude": 1,
                "what_it_means": (
                    f"OC v2026.5.26+ runs a syntactic preflight before exec "
                    "and rejects specific shapes (python/node, pipes, "
                    "redirects, `&&`, `-c`) regardless of allowlist. "
                    f"`{bot_id}` is running into this. Fix is either to "
                    "wrap the command in a shell script the allowlist "
                    "permits, or wait for the upstream OC fix."
                ),
                "fix_steps": (
                    "1. Inspect the sample input in the investigator's "
                    "Proposal — what shape is the bot using?\n"
                    "2. If `python` / `node`: wrap in a script under the "
                    f"bot's `~/.openclaw/workspace/scripts/` that the "
                    "allowlist permits, point the bot at the script\n"
                    "3. If pipes / redirects: replace with a single-purpose "
                    "shell script or two sequential approved commands\n"
                    "4. Track the upstream fix at https://github.com/"
                    "openclaw/openclaw/issues/87371"
                ),
            },
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    today: date | None = None,
    session_loader: Any = None,
) -> list[dict]:
    """Run all detectors for one bot. Returns observe() kwargs dicts.

    ``session_loader`` defaults to ``_default_session_loader`` which reads
    OC's per-bot session JSONL from disk. Tests inject a fake to keep
    test runs hermetic; production calls with ``session_loader=None`` and
    the loader resolves automatically.
    """
    thresholds = _thresholds_for_bot(bot_id, config)

    # Phase 1 source: annotation JSONL
    window_days = int(thresholds["tool_error_burst_window_days"])
    annotations = read_turn_annotations(
        shared_dir, bot_id, days=window_days, today=today,
    )

    detections: list[dict] = []
    detections += detect_tool_error_burst(
        bot_id,
        annotations,
        window_days=window_days,
        min_count=int(thresholds["tool_error_burst_min_count"]),
        min_sessions=int(thresholds["tool_error_burst_min_sessions"]),
        max_per_run=int(thresholds["tool_error_burst_max_per_run"]),
    )

    # Phase 2 source: OC session content via session_loader. Default to
    # the on-disk OC session JSONL loader; tests inject a fake to keep
    # runs hermetic.
    if session_loader is None:
        session_loader = _default_session_loader
    outcomes = _iter_failed_tool_outcomes(session_loader, bot_id, annotations)

    detections += detect_exec_denied(
        bot_id,
        outcomes,
        window_days=int(thresholds["exec_denied_window_days"]),
        min_count=int(thresholds["exec_denied_min_count"]),
        max_per_run=int(thresholds["exec_denied_max_per_run"]),
    )
    detections += detect_approval_timeout(
        bot_id,
        outcomes,
        window_days=int(thresholds["approval_timeout_window_days"]),
        min_count=int(thresholds["approval_timeout_min_count"]),
        max_per_run=int(thresholds["approval_timeout_max_per_run"]),
    )
    detections += detect_preflight_block(
        bot_id,
        outcomes,
        window_days=int(thresholds["preflight_block_window_days"]),
        min_count=int(thresholds["preflight_block_min_count"]),
        max_per_run=int(thresholds["preflight_block_max_per_run"]),
    )
    return detections


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    today: date | None = None,
    session_loader: Any = None,
) -> tuple[set[str], int]:
    """Collect detections, write Signals, return (kept_signatures, count).

    Mirrors cost_watchdog.run_for_bot — same breaker suppression, same
    fail-open semantics. Distinct producer, distinct signal types.
    """
    detections = collect_for_bot(
        bot_id, shared_dir, config, today=today, session_loader=session_loader,
    )
    try:
        from breakers.suppression import find_suppressing_breaker
    except Exception as exc:  # noqa: BLE001
        print(
            f"[exec_outcome_watchdog] suppression import failed; "
            f"proceeding without suppression: {exc}",
            flush=True,
        )
        find_suppressing_breaker = None  # type: ignore[assignment]

    kept: set[str] = set()
    for d in detections:
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue

        sup_rec = None
        if find_suppressing_breaker is not None:
            category = _SUPPRESSIBLE_TYPES_TO_CATEGORY.get(d.get("type", ""))
            if category is not None:
                try:
                    sup_rec = find_suppressing_breaker(
                        shared_dir, bot_id, category=category,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[exec_outcome_watchdog] suppression check raised "
                        f"for {d['signature']}; proceeding: {exc}",
                        flush=True,
                    )
                    sup_rec = None
        if sup_rec is not None:
            print(
                f"[exec_outcome_watchdog] suppressed {d['signature']} "
                f"(breaker={sup_rec.type} scope={sup_rec.bot_id} "
                f"trip_id={sup_rec.trip_id[:8]})",
                flush=True,
            )
            continue

        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[exec_outcome_watchdog] observe failed for "
                f"{d['signature']}: {exc}",
                flush=True,
            )
    return kept, len(detections)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="exec_outcome_watchdog — Signal bot exec-outcome failures",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    primary = get_primary(config)
    members = get_members(config)
    all_bots = ([primary] if primary and primary not in members else []) + members
    all_bots = [b for b in all_bots if b]
    if args.bot:
        all_bots = [args.bot]

    all_kept: set[str] = set()
    total = 0
    for bot in all_bots:
        kept, n = run_for_bot(bot, shared_dir, config, dry_run=args.dry_run)
        all_kept |= kept
        total += n

    if args.dry_run:
        print(
            f"[exec_outcome_watchdog] dry-run: {len(all_bots)} bots, "
            f"{total} would-fire",
            flush=True,
        )
        return

    try:
        resolved = signals_store.sweep_resolve(
            shared_dir, producer=PRODUCER, kept_signatures=all_kept,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[exec_outcome_watchdog] sweep_resolve failed: {exc}",
            flush=True,
        )
        resolved = 0
    print(
        f"[exec_outcome_watchdog] ran for {len(all_bots)} bots, "
        f"fired {total}, archived {resolved}",
        flush=True,
    )


if __name__ == "__main__":
    main()
