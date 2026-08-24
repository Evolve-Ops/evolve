"""app_script_failure_audit — Signal producer for app scripts that FAIL at runtime.

Why this exists
---------------

Sibling to :mod:`agent_bypass_audit`. Both close visibility gaps in the
agent-freelance bypass class documented in
``internal/spec-agent-freelance-bypass-2026-06-05.md``, and both walk the same
session transcripts for the same set of at-risk apps (apps whose
``bot_guidance`` routes a chat trigger through a bot-local script).

``agent_bypass_audit`` catches the *never-invoked* case: a triggering
message arrived but the declared script was not run at all. It does NOT
catch the *invoked-but-failed* case — the OpenClaw exec failure chip
(``⚠️ 🔨 run python3 …/<script>.py (agent) failed``) that leaks into the
conversation when an agent-invoked script errors out. That's the gap this
producer closes.

The shape is the 2026-05-24 Slack failure that motivated the whole
``invocation_mode`` work (``run python3 ops/tools/unified_task_system.py
(agent) failed``; see ``internal/spec-app-derived-permissions-2026-05-24.md``):
the agent invoked the declared script, OpenClaw's exec subsystem failed it,
the agent fell back to its general tools and answered freelance — skipping
the script's controls (scope gates, source grounding, opt-out honor,
per-sender rate limit, privacy log) — and the only trace the operator ever
saw was a ``(agent) failed`` chip buried in a chat thread. A real
``scripts/tasks.py`` failure on bot ``personal-bot`` leaked this way and no Signal
fired.

What it does
------------

Once per day, in the same daily window as ``agent_bypass_audit`` (it runs
from the same ``run_agent_bypass_audit.py`` daemon wrapper):

  1. For each bot with at-risk apps installed, enumerate the apps and their
     declared script basenames via
     ``agent_bypass_audit._installed_at_risk_apps_for_bot`` (manifest
     ``event_triggers[]`` driven, with the hardcoded catalogue as fallback).

  2. For each (bot, app), walk the bot's session transcripts in the lookback
     window (reusing ``agent_bypass_audit``'s transcript-read helpers — the
     evolve user already has ACL read on ``/Users/<bot>/.openclaw/``) and
     count sessions carrying the ``(agent) failed`` exec-failure marker on a
     line that also names the app's script basename.

  3. Per (bot, app) with ≥1 failed invocation over the window, emit one
     Signal (type ``app_script_failure``) via ``signals.store.observe()``
     (find-or-create + signature dedup). Body cites the count, an example
     chip, and the remediation (migrate the app to
     ``invocation_mode: plugin_intercept`` and fix the script).

  4. ``signals.store.sweep_resolve()`` clears prior Signals for (bot, app)
     pairs whose failure count over the current window is zero — the
     operator migrated/fixed the app, so the alert auto-archives.

Producer:  ``app_script_failure_audit``
Signal:    ``app_script_failure``
Signature: ``app_script_failure_audit:app_script_failure:{bot_id}:{app_id}``
Scope:     pod (one Signal per (bot, app); operator triages each)
Severity:  warn — inherited from the central producer-severity map
           (``signals.producer_severity``). Behavioral integrity is
           operationally important but not page-the-operator urgent.

Pure Python, no LLM. Runs as the ``evolve`` user. Daily cadence. Idempotent
via ``observe`` (same signature → existing Signal updated) and
``sweep_resolve`` (failure cleared → Signal archived).

Detection notes
---------------

Session-scoped, like the bypass audit: a session with one or more
``(agent) failed`` chips for the app's script counts as one failure
session; the raw chip count is also tracked. Anchoring on the literal
``(agent) failed`` marker (the artifact that actually leaked into chat) on
the *same line* as the script basename keeps attribution precise — a
benign nonzero exit that OpenClaw doesn't surface as the agent-failure chip
is not counted, and a failure for app A's script isn't attributed to app B.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR
from evolve_config import bot_home as _bot_home
from schema.signal import make_signature
from signals import store as signals_store

# Reuse the transcript-read + at-risk-app discovery path from the bypass
# audit. These are the "same transcript walk" the spec calls for — the
# import keeps a single source of truth for session enumeration, transcript
# probing, and manifest-driven app discovery.
from agent_bypass_audit import (  # noqa: F401 — re-exported for test patch targets
    DEFAULT_LOOKBACK_HOURS,
    MAX_SESSIONS_PER_BOT_APP,
    AtRiskApp,
    _installed_at_risk_apps_for_bot,
    _read_transcript,
    _session_ids_in_window,
    _window_dates,
)


PRODUCER = "app_script_failure_audit"
SIGNAL_TYPE = "app_script_failure"


# The OpenClaw exec-failure chip that leaks into chat when an agent-invoked
# script fails: "⚠️ 🔨 run python3 …/<script>.py (agent) failed". The stable,
# format-independent anchor is the "(agent) failed" tail; the script
# basename on the same line attributes the failure to the right (bot, app).
# Case-insensitive + flexible whitespace so a rendering tweak upstream
# (emoji, spacing, "failed:" with a trailing reason) doesn't silently
# regress detection.
_FAILURE_MARKER_RE = re.compile(r"\(agent\)\s+failed\b", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Transcript text flattening
# ─────────────────────────────────────────────────────────────────────────────


def _flatten_content_text(content) -> list[str]:
    """Collect every human-readable text fragment from a message ``content``.

    Broader than ``agent_bypass_audit._text_of_content`` (which only reads
    ``type == "text"`` blocks on user messages): the ``(agent) failed`` chip
    can surface as an assistant text block, a plain-string message, or a
    ``tool_result`` block's content, so we walk all of them. Non-text blocks
    (``tool_use`` inputs, images) contribute nothing.
    """
    out: list[str] = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "tool_result":
                    out.extend(_flatten_content_text(block.get("content")))
                elif isinstance(block.get("text"), str):
                    # Covers type == "text" and any text-bearing block shape.
                    out.append(block["text"])
    return out


def _all_text_of_record(rec: dict) -> str:
    """Flatten an entire transcript record (any role) into searchable text."""
    parts = _flatten_content_text(rec.get("content"))
    # Some transcript shapes carry a top-level rendered string.
    for key in ("text", "message"):
        v = rec.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────────
# Per-session audit
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionFailureResult:
    session_id: str
    failures_seen: int = 0
    example_text: str = ""

    @property
    def has_failure(self) -> bool:
        return self.failures_seen > 0


def _audit_one_session_failures(
    records: list[dict],
    app: AtRiskApp,
) -> SessionFailureResult:
    """Count ``(agent) failed`` chips naming ``app.script_basename``.

    Same-line requirement (basename AND marker on one line) matches the
    single-line chip format and avoids cross-attribution between apps.
    """
    result = SessionFailureResult(session_id="")  # caller sets id
    for rec in records:
        text = _all_text_of_record(rec)
        if not text or app.script_basename not in text:
            continue
        for line in text.splitlines():
            if app.script_basename in line and _FAILURE_MARKER_RE.search(line):
                result.failures_seen += 1
                if not result.example_text:
                    result.example_text = line.strip()[:240]
    return result


@dataclass
class BotAppFailureTotals:
    bot_id: str
    app_id: str
    script_basename: str = ""
    sessions_audited: int = 0
    failure_sessions: int = 0
    failures_total: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.failure_sessions == 0


# ─────────────────────────────────────────────────────────────────────────────
# Per-(bot, app) collection
# ─────────────────────────────────────────────────────────────────────────────


def _audit_bot_app_failures(
    shared_dir: Path,
    bot_id: str,
    network: dict,
    app: AtRiskApp,
    *,
    now: datetime,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> BotAppFailureTotals:
    """Run the failure audit for one (bot, app) pair over the lookback window."""
    totals = BotAppFailureTotals(
        bot_id=bot_id, app_id=app.app_id, script_basename=app.script_basename,
    )
    cutoff = now - timedelta(hours=lookback_hours)
    dates = _window_dates(now, lookback_hours)

    session_ids = _session_ids_in_window(
        shared_dir, bot_id, cutoff=cutoff, window_dates=dates,
    )
    if not session_ids:
        return totals

    # Cap before reading transcripts so the daemon's runtime is bounded even
    # when a bot has thousands of sessions in 24h (mirrors the bypass audit).
    session_ids = session_ids[:MAX_SESSIONS_PER_BOT_APP]

    home = _bot_home(bot_id, network)

    for sid in session_ids:
        records = _read_transcript(home, sid)
        if not records:
            continue
        per_sess = _audit_one_session_failures(records, app)
        per_sess.session_id = sid

        totals.sessions_audited += 1
        totals.failures_total += per_sess.failures_seen
        if per_sess.has_failure:
            totals.failure_sessions += 1
            if per_sess.example_text and len(totals.examples) < 3:
                totals.examples.append(
                    f"session {sid[:8]}: {per_sess.failures_seen} failure(s) — "
                    f"e.g. {per_sess.example_text!r}"
                )
    return totals


# ─────────────────────────────────────────────────────────────────────────────
# Signal spec builder
# ─────────────────────────────────────────────────────────────────────────────


def _spec_for_failure(totals: BotAppFailureTotals, lookback_hours: int) -> dict:
    """Render a Signal spec for one (bot, app) with failed invocations.

    Severity/flavor are intentionally omitted — ``observe`` fills them from
    the central per-producer policy in ``signals.producer_severity``
    (``app_script_failure_audit`` → warn / maintenance). Category likewise
    derives from ``schema.signal.PRODUCER_CATEGORY_DEFAULT`` (→ platform).
    """
    signature = make_signature(
        PRODUCER, SIGNAL_TYPE,
        scope_key=f"{totals.bot_id}:{totals.app_id}",
    )

    title = (
        f"{totals.app_id} on {totals.bot_id}: "
        f"{totals.failure_sessions} session(s) where the app script failed"
    )

    body_lines = [
        f"**{totals.app_id}** on bot **{totals.bot_id}** had "
        f"**{totals.failure_sessions} session(s)** in the last "
        f"{lookback_hours}h where the declared script "
        f"(`{totals.script_basename}`) was invoked but **failed** — the "
        f"`(agent) failed` marker leaked into the conversation. When an "
        f"agent-invoked app script fails, the agent typically falls back to "
        f"its general tools and answers freelance, bypassing the script's "
        f"controls (scope gates, source grounding, opt-out honor, rate limit).",
        "",
        f"Window totals: {totals.failures_total} failed invocation(s) across "
        f"{totals.sessions_audited} session(s) audited.",
        "",
    ]
    if totals.examples:
        body_lines.append("Example failures:")
        for ex in totals.examples:
            body_lines.append(f"  - {ex}")
        body_lines.append("")

    body_lines.append(
        "Fix: migrate this app to `invocation_mode: plugin_intercept` so the "
        "plugin runs the script deterministically (a failure surfaces as a "
        "structured error instead of an agent freelance), and fix the "
        "underlying script error. See "
        "`internal/spec-agent-freelance-bypass-2026-06-05.md` for the gap and the "
        "phased remediation plan."
    )

    return dict(
        signature=signature,
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details={
            "bot_id":           totals.bot_id,
            "app_id":           totals.app_id,
            "script_basename":  totals.script_basename,
            "lookback_hours":   lookback_hours,
            "sessions_audited": totals.sessions_audited,
            "failure_sessions": totals.failure_sessions,
            "failures_total":   totals.failures_total,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    shared_dir: Path,
    *,
    network: dict | None = None,
    now: datetime | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> list[dict]:
    """Walk every bot, return Signal specs for (bot, app) with failed invocations."""
    if network is None:
        try:
            network = json.loads(
                (shared_dir / "network.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            network = {}

    if now is None:
        now = datetime.now(timezone.utc)

    bots = (network or {}).get("bots") or {}
    specs: list[dict] = []
    for bot_id in bots:
        installed = _installed_at_risk_apps_for_bot(bot_id)
        if not installed:
            continue
        for app in installed:
            totals = _audit_bot_app_failures(
                shared_dir,
                bot_id,
                network or {},
                app,
                now=now,
                lookback_hours=lookback_hours,
            )
            if totals.failure_sessions > 0:
                specs.append(_spec_for_failure(totals, lookback_hours))
    return specs


def run(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    network: dict | None = None,
    now: datetime | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> tuple[set[str], int, int]:
    """Collect specs, write Signals, sweep-resolve cleared (bot, app) pairs.

    Returns ``(kept_signatures, n_fired, n_resolved)`` — same shape as every
    other Signal-producing monitor under packages/analyzer/.
    """
    specs = collect(
        shared_dir,
        network=network,
        now=now,
        lookback_hours=lookback_hours,
    )
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{PRODUCER}] observe failed for {spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: no failed app-script invocations in current window",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{PRODUCER}] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"{PRODUCER} — daily Signal producer for app scripts that fail "
            "when an agent invokes them."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        # Platform-keyed default (Linux-port-safe) — avoids a hardcoded
        # macOS /Users/ literal. The launchd installer passes --shared-dir
        # explicitly, so this only affects ad-hoc CLI invocation.
        default=CANONICAL_SHARED_DIR,
        help="Pod-wide shared dir (default: the platform-canonical shared dir).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Audit window in hours (default: {DEFAULT_LOOKBACK_HOURS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="No-op flag for compatibility with the standard launchd invocation.",
    )
    args = parser.parse_args(argv)

    kept, n_fired, n_resolved = run(
        args.shared_dir,
        dry_run=args.dry_run,
        lookback_hours=args.lookback_hours,
    )
    print(
        f"[{PRODUCER}] fired={n_fired} resolved={n_resolved} kept={len(kept)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
