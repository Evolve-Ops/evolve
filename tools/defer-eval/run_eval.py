#!/usr/bin/env python3
"""
run_eval.py — measure how reliably a bot calls `defer` across varied prompts.

Background: the Continuity Engine v2 puts the bot in charge of detecting
when a defer is needed. That works only as well as the model decides it
does. This script runs a fixed prompt set against a real bot's gateway
and scores:

  - True-positive rate (TPR): of prompts that SHOULD defer, what % did?
  - False-positive rate (FPR): of prompts that should NOT defer, what % did?
  - Time accuracy: of correctly-fired defers with an expected offset,
    what % of fires_at values land within the tolerance window?
  - Mode accuracy: of correctly-fired defers, what % chose the right
    mode (message vs action)?

Run it after every system-prompt change and after every model upgrade
to track regressions.

Usage on the mini (where the bot's gateway runs):

    cd /Users/Shared/evolve-repo
    sudo -u evolve python3 tools/defer-eval/run_eval.py \
        --bot admin_bot \
        --prompts tools/defer-eval/prompts.json \
        --report /tmp/defer-eval-$(date +%Y%m%d-%H%M%S).json

Each prompt runs against a dedicated explicit session (default key
`defer-eval-<bot>-<YYYYMMDD-HHMMSS>`). The harness calls the gateway
RPC `sessions.reset` BEFORE each prompt so the bot sees every prompt
as a fresh first turn — same plugin tool catalog, same system prompt
injection (pod conduct, skills snapshot), no conversation-history
bleed. After all prompts run, the harness calls `sessions.delete` to
remove the session entry and archive the transcript (override with
--keep-session for postmortem).

Important operational notes:

  * The script invokes `openclaw agent` for each prompt. Each invocation
    is a real model call (~$0.02-0.05 of API spend). 40 prompts ≈ $1-2.
    Don't run during peak operator hours; ~5-15 min wall time.

  * `--deliver` is NOT passed: the bot's reply does NOT go to Telegram.
    However, if the bot calls defer with mode=message, the row is
    queued and (if not cleaned up) WILL fire to the user's channel
    later. The script writes each prompt's row to the queue, captures
    it, then removes it (in-place rewrite under the queue's own lock)
    to keep noise out of the user's Telegram.

  * IMPORTANT — queue file ownership: the bot's defer plugin (running
    as the bot user) writes to defer-queue.jsonl, so the file MUST be
    owned by the bot. If a prior eval run rewrote the file via
    tempfile-rename (the broken legacy path), it ended up evolve-owned
    and the bot's defer tool fails silently with permission errors.
    The current cleanup uses in-place truncate-and-write under the
    queue's flock, which preserves the file's existing ownership.
    If you suspect a previous run broke ownership, fix it once with:
        ssh mini sudo /usr/sbin/chown <bot>:staff \\
            /Users/<bot>/.openclaw/workspace/evolve/defer-queue.jsonl
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Resolve bot macOS account via network.json if evolve_admin is importable.
# bot_id (logical name) may differ from the account name (e.g. team_bot_b/personal_bot_user).
try:
    from evolve_admin.config import bot_home as _ea_bot_home
    _EA_AVAILABLE = True
except ImportError:
    _ea_bot_home = None  # type: ignore[assignment]
    _EA_AVAILABLE = False


def _bot_home(bot_id: str) -> Path:
    """Resolve a bot's home directory, respecting user overrides in network.json."""
    if _EA_AVAILABLE and _ea_bot_home is not None:
        try:
            return _ea_bot_home(bot_id)
        except Exception:
            pass
    return Path(f"/Users/{bot_id}")


# ── Defaults ─────────────────────────────────────────────────────────────


# Per-prompt timeout for the openclaw agent invocation. Realistic agent
# runs against a freshly-reset session take ~15-30s (no transcript
# replay, simple prompt). 90s gives headroom for the model call,
# plugin bootstrap, and an occasional retry.
DEFAULT_AGENT_TIMEOUT_SEC = 90

# Timeout for the gateway RPC calls (sessions.reset / sessions.delete).
# These should respond in <1s; 15s is a generous ceiling.
DEFAULT_GATEWAY_RPC_TIMEOUT_SEC = 15

# How long we wait between agent run completion and queue inspection.
# The plugin writes to defer-queue.jsonl synchronously inside the tool
# execute() callback, so 1-2s is plenty.
QUEUE_INSPECT_DELAY_SEC = 2

# Default agent id for an OpenClaw bot. Most bots only have a "main"
# agent; pass --agent-id to override (rarely needed).
DEFAULT_AGENT_ID = "main"


# ── Prompt + result dataclasses ──────────────────────────────────────────


@dataclass
class PromptCase:
    id: str
    category: str
    subcategory: str
    text: str
    expected_called_defer: bool
    expected_mode: Optional[str]
    expected_due_at_offset_minutes: Optional[float]
    expected_due_at_tolerance_minutes: Optional[float]


@dataclass
class CaseResult:
    prompt_id: str
    category: str
    text: str
    pass_overall: bool = False
    pass_called_defer: bool = False
    pass_mode: Optional[bool] = None
    pass_due_at: Optional[bool] = None
    actual_called_defer: bool = False
    actual_mode: Optional[str] = None
    actual_fires_at: Optional[str] = None
    actual_due_at_offset_minutes: Optional[float] = None
    agent_run_ok: bool = False
    agent_run_duration_ms: int = 0
    agent_response_excerpt: str = ""
    error: str = ""
    # Per-prompt session reset (approach a — eliminates context bleed).
    # If reset fails, we record the error and run the prompt anyway, so
    # the operator can see whether the result is still meaningful.
    session_reset_ok: bool = True
    session_reset_error: str = ""


@dataclass
class EvalReport:
    started_at: str
    finished_at: str = ""
    bot_id: str = ""
    prompt_set: str = ""
    session_id: str = ""
    session_key: str = ""
    agent_id: str = ""
    reset_between_prompts: bool = True
    session_reset_failures: int = 0
    total: int = 0
    should_defer_total: int = 0
    should_not_defer_total: int = 0
    true_positives: int = 0
    false_negatives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    mode_correct: int = 0
    mode_total_eligible: int = 0
    due_at_correct: int = 0
    due_at_total_eligible: int = 0
    agent_run_failures: int = 0
    cases: list[CaseResult] = field(default_factory=list)


# ── Prompt loading ───────────────────────────────────────────────────────


def load_prompts(path: Path) -> list[PromptCase]:
    raw = json.loads(path.read_text())
    out: list[PromptCase] = []
    for item in raw.get("prompts", []):
        exp = item.get("expected", {})
        out.append(PromptCase(
            id=item["id"],
            category=item["category"],
            subcategory=item.get("subcategory", ""),
            text=item["text"],
            expected_called_defer=bool(exp.get("called_defer", False)),
            expected_mode=exp.get("mode"),
            expected_due_at_offset_minutes=exp.get("due_at_offset_minutes"),
            expected_due_at_tolerance_minutes=exp.get("due_at_tolerance_minutes"),
        ))
    return out


# ── Per-bot queue access ─────────────────────────────────────────────────


def queue_path(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "evolve" / "defer-queue.jsonl"


def lock_path(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace" / "evolve" / "defer-queue.jsonl.lock"


def read_queue(bot_id: str) -> list[dict]:
    """Return all rows currently in the bot's defer queue, by direct file
    read. We're root (or evolve) when running this; queue is per-bot."""
    p = queue_path(bot_id)
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def remove_rows_by_id(bot_id: str, defer_ids: set[str]) -> int:
    """Rewrite the bot's queue file in-place with the given defer_ids
    removed. Returns the number of rows removed.

    Takes the same lockfile the bot's defer plugin takes, so concurrent
    writers (the bot's defer tool) serialize on it.

    IMPORTANT — uses in-place truncate+write (`open(path, "w")`) rather
    than the more idiomatic tempfile + os.replace pattern. Reason: the
    queue file MUST stay owned by the bot user, since the bot's defer
    tool runs as that user and writes new rows directly. A tempfile
    rename — done while the eval runs as `evolve` — would create the
    new file under evolve and break every subsequent defer write
    (silent permission failures: tool reports success, no row appears).
    The eval process holds the file's flock while writing, so partial
    writes can't race with the bot's writer.
    """
    if not defer_ids:
        return 0
    qp = queue_path(bot_id)
    if not qp.exists():
        return 0
    lp = lock_path(bot_id)
    lp.parent.mkdir(parents=True, exist_ok=True)
    # Match the queue's own lock semantics (mode 0o666 in defer_queue.py).
    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        kept: list[str] = []
        removed = 0
        for line in qp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if d.get("defer_id") in defer_ids:
                removed += 1
            else:
                kept.append(line)
        # In-place rewrite — preserves file ownership (see docstring).
        with open(qp, "w") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        return removed
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ── Session lifecycle (gateway RPC) ──────────────────────────────────────


def session_key_for(agent_id: str, session_id: str) -> str:
    """Build the canonical session key OpenClaw uses for explicit
    sessions. The `openclaw agent --session-id <id>` CLI constructs
    the same key internally; we need it explicitly here so we can pass
    it to gateway RPCs (sessions.reset, sessions.delete, tools.effective).
    """
    return f"agent:{agent_id}:explicit:{session_id}"


def _gateway_call(
    bot_id: str,
    method: str,
    params: dict,
    *,
    timeout_sec: int = DEFAULT_GATEWAY_RPC_TIMEOUT_SEC,
    openclaw_bin: str = "/opt/homebrew/bin/openclaw",
) -> tuple[bool, dict, str]:
    """Invoke `openclaw gateway call <method>` as the bot user. Returns
    (ok, parsed_response_object, error_text)."""
    cmd = [
        "sudo", "-H", "-u", bot_id,
        openclaw_bin, "gateway", "call", method,
        "--params", json.dumps(params),
        "--json",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired:
        return False, {}, f"gateway call {method} timed out after {timeout_sec}s"
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:500]
        return False, {}, f"gateway call {method} failed (rc={r.returncode}): {err}"
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False, {}, f"gateway call {method} stdout not JSON: {r.stdout[:200]}"
    if not out.get("ok", False):
        return False, out, f"gateway call {method} returned not-ok: {out!r}"
    return True, out, ""


def reset_session(
    bot_id: str,
    session_key: str,
    *,
    openclaw_bin: str = "/opt/homebrew/bin/openclaw",
) -> tuple[bool, str]:
    """Call `sessions.reset` to clear conversation history while keeping
    the same session key. The next agent run sees a fresh first turn:
    full system-prompt re-injection, full plugin-tool catalog, no prior
    transcript. Returns (ok, error_text).

    This is approach (a) from the design doc — eliminates context bleed
    across the 40-prompt eval without changing the bot's production
    setup. The session key stays stable; only the underlying sessionId
    + transcript file roll over (the previous transcript is archived
    by the gateway).
    """
    ok, _, err = _gateway_call(
        bot_id, "sessions.reset",
        {"key": session_key, "reason": "reset"},
        openclaw_bin=openclaw_bin,
    )
    return ok, err


def delete_session(
    bot_id: str,
    session_key: str,
    *,
    openclaw_bin: str = "/opt/homebrew/bin/openclaw",
) -> tuple[bool, str]:
    """Call `sessions.delete` to remove the session entry + archive the
    transcript. Used at end-of-eval to keep the per-bot session list
    tidy. Returns (ok, error_text)."""
    ok, _, err = _gateway_call(
        bot_id, "sessions.delete",
        {"key": session_key, "deleteTranscript": True},
        openclaw_bin=openclaw_bin,
    )
    return ok, err


# ── Agent dispatch ───────────────────────────────────────────────────────


def run_agent(
    bot_id: str,
    session_id: str,
    text: str,
    *,
    timeout_sec: int = DEFAULT_AGENT_TIMEOUT_SEC,
    openclaw_bin: str = "/opt/homebrew/bin/openclaw",
) -> tuple[bool, str, dict, int]:
    """Send `text` to the bot via `openclaw agent --session-id <id>`.
    Returns (ok, error_or_response_excerpt, parsed_json, duration_ms).

    No --deliver: the agent's reply does NOT go to the user's channel.

    The eval main loop calls `reset_session` BEFORE each prompt, so
    every invocation here sees a freshly-cleared session — same plugin
    tool catalog and same system-prompt injection as a real first turn,
    but no transcript bleed from the previous prompt. The session key
    is `agent:<agent_id>:explicit:<session_id>` (see session_key_for).

    `sudo -H -u <bot>` from evolve is required because openclaw reads
    gateway-auth tokens from the invoking user's HOME, and evolve has
    none for the bot. -H makes sudo set HOME to the bot's.
    """
    cmd = [
        "sudo", "-H", "-u", bot_id,
        openclaw_bin, "agent",
        "--session-id", session_id,
        "--message", text,
        "--json",
    ]
    started_ms = int(time.time() * 1000)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd="/tmp",
        )
    except subprocess.TimeoutExpired:
        return False, f"agent timed out after {timeout_sec}s", {}, int(time.time() * 1000) - started_ms
    duration_ms = int(time.time() * 1000) - started_ms
    if r.returncode != 0:
        err = (r.stderr.strip() or r.stdout.strip())[:500]
        return False, f"agent failed (rc={r.returncode}): {err}", {}, duration_ms
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False, f"agent stdout not JSON: {r.stdout[:200]}", {}, duration_ms
    excerpt = ""
    try:
        payloads = parsed.get("result", {}).get("payloads") or []
        if payloads:
            excerpt = (payloads[0].get("text") or "")[:200]
    except Exception:
        pass
    return True, excerpt, parsed, duration_ms


# ── Scoring ──────────────────────────────────────────────────────────────


def score_case(
    case: PromptCase,
    queue_before: set[str],
    queue_after: list[dict],
    prompt_sent_at: _dt.datetime,
    agent_ok: bool,
    agent_excerpt: str,
    agent_duration_ms: int,
) -> tuple[CaseResult, set[str]]:
    """Compare actual vs expected behavior for one prompt. Returns (result,
    new_defer_ids_to_clean_up)."""
    res = CaseResult(
        prompt_id=case.id,
        category=case.category,
        text=case.text,
        agent_run_ok=agent_ok,
        agent_run_duration_ms=agent_duration_ms,
        agent_response_excerpt=agent_excerpt,
    )
    if not agent_ok:
        res.error = agent_excerpt
        return res, set()

    new_rows = [r for r in queue_after if r.get("defer_id") not in queue_before]
    res.actual_called_defer = len(new_rows) > 0

    # called_defer pass/fail
    res.pass_called_defer = (res.actual_called_defer == case.expected_called_defer)

    new_ids = {r.get("defer_id") for r in new_rows if r.get("defer_id")}

    if not res.actual_called_defer:
        # No row written — pass iff we didn't expect one
        res.pass_overall = res.pass_called_defer
        return res, new_ids

    # Row was written. If we expected NO row, that's a false positive.
    if not case.expected_called_defer:
        res.pass_overall = False
        return res, new_ids

    # Expected a row + got one. Score mode + due_at if asserted.
    row = new_rows[0]   # if multiple, score the first; warn on >1 below
    res.actual_mode = row.get("mode")
    res.actual_fires_at = row.get("fires_at")

    # Mode check
    if case.expected_mode is not None:
        res.pass_mode = (res.actual_mode == case.expected_mode)
    else:
        res.pass_mode = True   # any mode acceptable when expected_mode unspecified

    # Due-at check
    if (
        case.expected_due_at_offset_minutes is not None
        and res.actual_fires_at
    ):
        try:
            fires_at = _dt.datetime.fromisoformat(res.actual_fires_at.replace("Z", "+00:00"))
            if fires_at.tzinfo is None:
                fires_at = fires_at.replace(tzinfo=_dt.timezone.utc)
            offset_min = (fires_at - prompt_sent_at).total_seconds() / 60.0
            res.actual_due_at_offset_minutes = round(offset_min, 2)
            tol = case.expected_due_at_tolerance_minutes or 1.0
            res.pass_due_at = abs(offset_min - case.expected_due_at_offset_minutes) <= tol
        except (ValueError, TypeError):
            res.pass_due_at = False
    elif case.expected_due_at_offset_minutes is None:
        res.pass_due_at = True   # not asserted

    res.pass_overall = (
        res.pass_called_defer
        and (res.pass_mode is True or res.pass_mode is None)
        and (res.pass_due_at is True or res.pass_due_at is None)
    )
    return res, new_ids


# ── Aggregate + render ───────────────────────────────────────────────────


def aggregate(report: EvalReport) -> None:
    """Fill in summary counters from report.cases."""
    for c in report.cases:
        if c.category == "should_defer":
            report.should_defer_total += 1
            if c.actual_called_defer:
                report.true_positives += 1
            else:
                report.false_negatives += 1
            # Mode + due_at scoring is only eligible when called_defer
            if c.actual_called_defer:
                if c.pass_mode is not None:
                    report.mode_total_eligible += 1
                    if c.pass_mode:
                        report.mode_correct += 1
                if c.pass_due_at is not None:
                    report.due_at_total_eligible += 1
                    if c.pass_due_at:
                        report.due_at_correct += 1
        else:
            report.should_not_defer_total += 1
            if c.actual_called_defer:
                report.false_positives += 1
            else:
                report.true_negatives += 1
        if not c.agent_run_ok:
            report.agent_run_failures += 1
        if not c.session_reset_ok:
            report.session_reset_failures += 1
    report.total = len(report.cases)


def render_summary(report: EvalReport) -> str:
    def pct(num: int, denom: int) -> str:
        if denom == 0:
            return "n/a"
        return f"{(100.0 * num / denom):.1f}%"

    lines = [
        f"defer-eval report — bot={report.bot_id} prompts={report.prompt_set}",
        f"started:  {report.started_at}",
        f"finished: {report.finished_at}",
        "",
        f"total prompts: {report.total}",
        f"agent-run failures: {report.agent_run_failures}",
        f"session-reset failures: {report.session_reset_failures}",
        "",
        "── should-defer (n={}) ──".format(report.should_defer_total),
        f"  TP (called defer):       {report.true_positives:3d}  "
        f"({pct(report.true_positives, report.should_defer_total)})",
        f"  FN (missed):             {report.false_negatives:3d}  "
        f"({pct(report.false_negatives, report.should_defer_total)})",
        "",
        "── should-NOT-defer (n={}) ──".format(report.should_not_defer_total),
        f"  TN (correctly skipped):  {report.true_negatives:3d}  "
        f"({pct(report.true_negatives, report.should_not_defer_total)})",
        f"  FP (called when not):    {report.false_positives:3d}  "
        f"({pct(report.false_positives, report.should_not_defer_total)})",
        "",
        "── mode + timing (within true-positives) ──",
        f"  mode correct:    {report.mode_correct:3d}/{report.mode_total_eligible}  "
        f"({pct(report.mode_correct, report.mode_total_eligible)})",
        f"  due_at correct:  {report.due_at_correct:3d}/{report.due_at_total_eligible}  "
        f"({pct(report.due_at_correct, report.due_at_total_eligible)})",
        "",
    ]
    failures = [c for c in report.cases if not c.pass_overall]
    if failures:
        lines.append(f"── failures (n={len(failures)}) ──")
        for c in failures:
            tags = []
            if not c.agent_run_ok:
                tags.append("RUN-FAIL")
            elif c.category == "should_defer" and not c.actual_called_defer:
                tags.append("MISSED")
            elif c.category == "should_not_defer" and c.actual_called_defer:
                tags.append("FALSE-POS")
            else:
                if c.pass_mode is False:
                    tags.append(f"MODE(want={c.actual_mode!s})")
                if c.pass_due_at is False:
                    tags.append(
                        f"TIME(actual={c.actual_due_at_offset_minutes}m)"
                    )
            lines.append(f"  {c.prompt_id}  [{','.join(tags)}]  {c.text[:70]!r}")
    return "\n".join(lines)


# ── Main loop ────────────────────────────────────────────────────────────


def run_eval(
    bot_id: str,
    prompts: list[PromptCase],
    *,
    session_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    cleanup: bool = True,
    reset_between_prompts: bool = True,
    agent_timeout_sec: int = DEFAULT_AGENT_TIMEOUT_SEC,
) -> EvalReport:
    started = _dt.datetime.now(_dt.timezone.utc)
    sk = session_key_for(agent_id, session_id)
    report = EvalReport(
        started_at=started.isoformat(timespec="seconds"),
        bot_id=bot_id,
        session_id=session_id,
        session_key=sk,
        agent_id=agent_id,
        reset_between_prompts=reset_between_prompts,
    )

    queue_before = {r.get("defer_id") for r in read_queue(bot_id) if r.get("defer_id")}

    for case in prompts:
        # Approach (a): reset the session BEFORE each prompt so the
        # bot sees a fresh first turn (no transcript bleed). The reset
        # is best-effort — if it fails we still run the prompt and
        # mark the case so the operator can see what happened.
        reset_ok, reset_err = (True, "")
        if reset_between_prompts:
            reset_ok, reset_err = reset_session(bot_id, sk)

        prompt_sent_at = _dt.datetime.now(_dt.timezone.utc)
        ok, excerpt, _parsed, dur_ms = run_agent(
            bot_id, session_id, case.text, timeout_sec=agent_timeout_sec,
        )
        # Brief settle — defer tool writes synchronously, but give the
        # filesystem a moment to flush before we read.
        time.sleep(QUEUE_INSPECT_DELAY_SEC)
        queue_after = read_queue(bot_id)
        result, new_ids = score_case(
            case, queue_before, queue_after, prompt_sent_at, ok, excerpt, dur_ms,
        )
        result.session_reset_ok = reset_ok
        result.session_reset_error = reset_err
        report.cases.append(result)

        # Print live progress so the operator can watch a long run.
        flag = "✓" if result.pass_overall else "✗"
        reset_tag = "" if reset_ok else " RESET-FAIL"
        print(
            f"  [{flag}] {case.id:25s} agent_ok={ok} "
            f"called_defer={result.actual_called_defer} "
            f"({dur_ms}ms){reset_tag}",
            flush=True,
        )

        # Clean up the row this prompt produced so the bot's user doesn't
        # get a stream of test-message defers later.
        if cleanup and new_ids:
            removed = remove_rows_by_id(bot_id, new_ids)
            if removed != len(new_ids):
                print(
                    f"    WARN: expected to remove {len(new_ids)} row(s), removed {removed}",
                    flush=True,
                )

        # Refresh known-ids so the next prompt's queue diff doesn't see
        # rows leftover from this prompt (in case cleanup didn't catch them).
        queue_before = {r.get("defer_id") for r in read_queue(bot_id) if r.get("defer_id")}

    report.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    aggregate(report)
    return report


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Defer-tool reliability eval")
    parser.add_argument("--bot", required=True, help="Bot id (e.g. admin_bot)")
    parser.add_argument(
        "--session-id",
        default=None,
        help=(
            "Session id label to run all prompts against. The session key "
            "becomes agent:<agent-id>:explicit:<session-id>. Default is auto-"
            "generated as defer-eval-<bot>-<YYYYMMDD-HHMMSS> — fresh each run, "
            "isolated from any production session. Override only if you want to "
            "re-use a particular label across runs (e.g., for a postmortem)."
        ),
    )
    parser.add_argument(
        "--agent-id",
        default=DEFAULT_AGENT_ID,
        help=f"Agent id within the bot (default: {DEFAULT_AGENT_ID})",
    )
    parser.add_argument(
        "--prompts",
        default=str(Path(__file__).parent / "prompts.json"),
        help="Path to prompts.json",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Where to write the JSON report. Default: stdout only.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Don't remove eval-produced rows from the queue after each prompt",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help=(
            "Don't reset the session before each prompt. Disables the "
            "context-bleed protection — only use when reproducing a "
            "specific bug where the bleed is part of the test."
        ),
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help=(
            "Don't delete the session at end-of-eval. Default is to "
            "sessions.delete the session key + archive its transcript "
            "so per-bot session lists stay tidy."
        ),
    )
    parser.add_argument(
        "--agent-timeout-sec",
        type=int,
        default=DEFAULT_AGENT_TIMEOUT_SEC,
        help="Per-prompt agent invocation timeout (default 90s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N prompts (smoke test)",
    )
    args = parser.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.exists():
        print(f"prompts file not found: {prompts_path}", file=sys.stderr)
        return 2
    prompts = load_prompts(prompts_path)
    if args.limit:
        prompts = prompts[: args.limit]

    session_id = args.session_id or _dt.datetime.now().strftime(
        f"defer-eval-{args.bot}-%Y%m%d-%H%M%S"
    )
    sk = session_key_for(args.agent_id, session_id)

    print(f"loaded {len(prompts)} prompts from {prompts_path}", flush=True)
    print(f"running against bot={args.bot} session_key={sk}", flush=True)
    print(
        f"reset_between_prompts={not args.no_reset} "
        f"keep_session={args.keep_session} cleanup={not args.no_cleanup}",
        flush=True,
    )

    report = run_eval(
        bot_id=args.bot,
        prompts=prompts,
        session_id=session_id,
        agent_id=args.agent_id,
        cleanup=not args.no_cleanup,
        reset_between_prompts=not args.no_reset,
        agent_timeout_sec=args.agent_timeout_sec,
    )
    report.prompt_set = str(prompts_path)

    # End-of-eval session cleanup. Best-effort: failure is logged but
    # doesn't change the exit code, since the eval results are still
    # valid even if the session entry stays around.
    if not args.keep_session:
        del_ok, del_err = delete_session(args.bot, sk)
        if not del_ok:
            print(f"WARN: sessions.delete failed: {del_err}", flush=True)

    print()
    print(render_summary(report))

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        # Convert dataclasses to dicts via dict-walk
        def _d(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _d(v) for k, v in obj.__dict__.items()}
            if isinstance(obj, list):
                return [_d(x) for x in obj]
            return obj
        rp.write_text(json.dumps(_d(report), indent=2))
        print(f"\nfull report → {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
