"""recent_turns — recent USER turns for the primary bot.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``{shared_dir}/metrics/{bot_id}/recent-transcripts.json``
written by ``packages/plugin/src/observer/RecentTranscriptCapture.ts``.

Privacy invariants (from `security_warden_capture_policy` memory and
the capture module's docstring, locked 2026-05-05):
  - Retention: ≤200 entries OR 48h, whichever caps first.
  - Roles: USER text only — assistant replies are not captured.
  - Per-bot opt-out via ``network.bots[bot_id].securityScanning = false``.
    A bot that opted out has no buffer; this function returns the
    empty list (not an error) so the LLM can say "no recent context
    available" cleanly.

The spec wording is "last N admin↔primary turns in this channel."
v1 returns USER turns only — the capture module deliberately doesn't
record assistant replies (privacy invariant), and the bot's own
conversation history is already in its session context. The user-
side text is what makes intake context envelopes useful (admin's
problem description, what they were trying to do); attaching one-
sided context is better than no context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def recent_turns(
    shared_dir: Path,
    *,
    bot_id: str,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return the most recent ``limit`` USER turns recorded for ``bot_id``.

    Returns ``{"turns": [{session_id, turn_index, ts, text}, ...],
    "count": N, "bot_id": str, "opted_out": bool}``.

    ``opted_out=True`` when the buffer file is absent — distinguishes
    "no recent activity" (file exists, empty list) from "scanning
    disabled" (file never written). The bot can render different
    messages.
    """
    capped_limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))

    if not bot_id:
        return {
            "turns": [],
            "count": 0,
            "bot_id": "",
            "opted_out": True,
        }

    path = Path(shared_dir) / "metrics" / bot_id / "recent-transcripts.json"
    if not path.exists():
        return {
            "turns": [],
            "count": 0,
            "bot_id": bot_id,
            "opted_out": True,
        }

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # File exists but is unreadable / malformed — treat as no
        # data rather than failing the bot's tool call.
        return {
            "turns": [],
            "count": 0,
            "bot_id": bot_id,
            "opted_out": False,
        }

    if not isinstance(raw, list):
        return {
            "turns": [],
            "count": 0,
            "bot_id": bot_id,
            "opted_out": False,
        }

    # The capture writes newest-last, so take the tail.
    tail = raw[-capped_limit:]
    return {
        "turns": tail,
        "count": len(tail),
        "bot_id": bot_id,
        "opted_out": False,
    }
