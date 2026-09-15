"""signals.session_budget_emit — convert TS-side session-budget breaker
files into Signals on the watchlist.

Spec: internal/spec-session-budget-cap-2026-05-31.md (PR C).

Why this exists:

  The plugin's SessionCostMonitor writes per-session breaker files at
  ``{shared_dir}/breakers/<bot_id>/session-<sid>.json`` when a session
  crosses its configured cost cap (``agents.defaults.sessionBudgetCapUsd``).
  The breaker file is what the pre-turn check reads to reject the next
  turn — that's the load-bearing artifact. The Signal is the
  operator-visible echo that lands on the Alerts page.

  The plugin (TypeScript) doesn't have a clean path to the Python signal
  store, and we don't want it to — the plugin's responsibilities are
  observation + breaker writes, not signal hygiene. This module is the
  Python-side converter that reads any session-budget breaker files and
  observes Signals for them via ``signals.store.observe``.

Idempotency:

  ``observe()`` already dedup's by signature, so calling this module
  multiple times for the same breaker file just bumps observation_count
  on the existing Signal. The signature uses the session_id, so each
  runaway session gets its own Signal.

  When the operator deletes the breaker file (or rotates the session),
  the cost_watchdog sweep_resolve pass will auto-resolve the matching
  Signal on the next run — same pattern as the rest of cost_watchdog's
  Signal types.

Designed to be called from cost_watchdog.run_for_bot as one more
detection collector. Returns the same shape (list of observe() kwargs
dicts) for symmetry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from schema.signal import make_signature


# Producer tag used in Signal signatures. Distinct from "cost_watchdog"
# because the source-of-truth is the plugin's breaker file rather than
# the Python detector — operators tracing a signal back to its origin
# should land on the plugin's monitor, not cost_watchdog's collectors.
PRODUCER = "session_cost_monitor"


def _read_session_breaker_files(
    shared_dir: Path, bot_id: str,
) -> list[dict[str, Any]]:
    """Return every session-budget breaker record on disk for ``bot_id``.

    Files live at ``{shared_dir}/breakers/<bot_id>/session-<sid>.json``.
    Fail-soft: any unreadable / unparseable file is skipped (treating
    it as if absent). The shape we expect mirrors what
    ``SessionCostMonitor.writeBreakerFile`` writes; minimum fields
    required are ``session_id``, ``cap_usd``, ``cost_usd``.
    """
    bot_dir = shared_dir / "breakers" / bot_id
    if not bot_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(bot_dir.glob("session-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") != "session_budget":
            continue
        # Minimum-field sanity check.
        sid = data.get("session_id")
        cap = data.get("cap_usd")
        cost = data.get("cost_usd")
        if (
            not isinstance(sid, str)
            or not isinstance(cap, (int, float))
            or not isinstance(cost, (int, float))
        ):
            continue
        out.append(data)
    return out


def collect_for_bot(
    bot_id: str, shared_dir: Path,
) -> list[dict[str, Any]]:
    """Return one observe() kwargs dict per active session-budget breaker.

    Caller (cost_watchdog.main) is responsible for actually invoking
    ``signals_store.observe(**d)`` and adding the signatures to its
    ``kept`` set so the sweep-resolver doesn't auto-resolve them while
    the breaker file is still on disk.

    The Signal shape mirrors other cost-family signals: per-bot scope,
    severity="alert" (this represents an active block on user activity),
    flavor="maintenance" (it's a guardrail trip, not active engagement).
    Title and body are operator-facing and follow the cost-alert style
    refined in PR E.
    """
    records = _read_session_breaker_files(shared_dir, bot_id)
    out: list[dict[str, Any]] = []
    for rec in records:
        sid = rec["session_id"]
        cap_usd = float(rec["cap_usd"])
        cost_usd = float(rec["cost_usd"])
        # Signature is per-session — multiple concurrent runaways on the
        # same bot each get their own Signal. session_id is treated as
        # an opaque identifier here.
        signature = make_signature(
            PRODUCER, "session_budget_exceeded", f"{bot_id}:{sid}"
        )
        title = (
            f"Session paused: ${cost_usd:.4f} over ${cap_usd:.2f} cap"
        )
        # Body carries the who/where/when context the cost-alert
        # enrichment in PR E established. Anything null comes through as
        # "(unknown)" so the operator sees the limit of attribution.
        user_id = rec.get("user_id")
        channel_id = rec.get("channel_id")
        channel_kind = rec.get("channel_kind")
        tripped_at = rec.get("tripped_at") or "(unknown)"
        body_lines = [
            f"Bot: {bot_id}",
            f"Session: {sid[:16]}…" if len(sid) > 16 else f"Session: {sid}",
            f"Cost: ${cost_usd:.4f} (cap ${cap_usd:.2f})",
            f"Tripped at: {tripped_at}",
            f"User: {user_id or '(unknown)'}",
            f"Channel: {channel_id or '(unknown)'} ({channel_kind or '(unknown)'})",
            "",
            "The next turn on this session is rejected with a "
            "budget-exceeded message. To resume, raise "
            "agents.defaults.sessionBudgetCapUsd in the bot's "
            "openclaw.json (or via the Customizations UI), then delete "
            f"breakers/{bot_id}/session-*.json for this session.",
        ]
        out.append({
            "signature": signature,
            "producer": PRODUCER,
            "type": "session_budget_exceeded",
            "flavor": "maintenance",
            "severity": "alert",
            "scope": "bot",
            "bot_id": bot_id,
            "title": title,
            "body": "\n".join(body_lines),
            "details": {
                "session_id": sid,
                "cost_usd": cost_usd,
                "cap_usd": cap_usd,
                "user_id": user_id,
                "channel_id": channel_id,
                "channel_kind": channel_kind,
                "tripped_at": tripped_at,
                "last_model": rec.get("last_model"),
                "last_provider": rec.get("last_provider"),
            },
        })
    return out
