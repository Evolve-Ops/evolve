"""
better_engine/hints.py — Contextual discovery hint generation (§20).

Generates rec-hints.json for each bot's workspace/evolve/ directory.
Called at end of each Engine refresh cycle (§11 step 14).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Recommendation

from .model import now_iso
from ..config import bot_home as _bot_home

_log = logging.getLogger(__name__)

# Recommendation types eligible for hint generation (§20.4). The legacy
# ``explore`` type was retired alongside the pipeline-unification arc;
# exploratory recs from generators/app_suggester surface as
# ``app_quality`` via the proposal_reader bridge.
_ELIGIBLE_TYPES = {"app_quality", "onboarding"}


def generate_triggers(rec: "Recommendation") -> list[str]:
    """Generate trigger keywords for a recommendation (§20.5).

    Extracts app names from app: tags, domain keywords from domain: tags,
    and key noun bigrams from the title. Returns sorted deduplicated list,
    max 15 triggers.
    """
    triggers: set[str] = set()

    # From tags
    for tag in rec.tags:
        if tag.startswith("app:"):
            app_name = tag[4:]
            triggers.add(app_name)                      # hyphenated form
            triggers.add(app_name.replace("-", " "))    # space-separated form

        if tag.startswith("domain:"):
            triggers.add(tag[7:])                       # e.g. "health", "finance"

    # From title — extract key nouns and bigrams
    words = rec.title.lower().split()
    for i, w in enumerate(words):
        # Strip punctuation for comparison but keep original for trigger
        clean = w.strip(".,!?:;'\"()")
        if len(clean) > 5:
            triggers.add(clean)
        if i < len(words) - 1:
            next_w = words[i + 1].strip(".,!?:;'\"()")
            triggers.add(f"{clean} {next_w}")

    # Sort, deduplicate, max 15
    result = sorted(t for t in triggers if t)
    return result[:15]


def generate_hint_text(rec: "Recommendation") -> str:
    """Generate short instruction text for system prompt injection (§20.2).

    Returns 1-2 sentences describing the recommendation for the model.
    """
    if rec.type == "app_quality":
        return (
            f"A pending recommendation may be relevant to this conversation: "
            f'"{rec.title} — {rec.detail}" '
            "Mention this lightly if the user brings up the relevant app or topic. "
            "End with: \"type 'evo' if you'd like to look at that.\" "
            "Do not force it."
        )
    elif rec.type == "onboarding":
        return (
            f"A setup task is pending: \"{rec.title}.\" "
            "If the user's question relates to this capability, you can mention "
            "it once and suggest they type 'evo' to proceed."
        )
    else:
        return (
            f'A pending recommendation may be relevant: "{rec.title}." '
            "Surface it naturally if appropriate, then suggest 'evo' to act on it."
        )


def build_hints_file(recs: list["Recommendation"], bot_id: str) -> dict:
    """Build the full hints dict for one bot.

    Filters to eligible types, pending status, with a delivery path.
    Returns the rec-hints.json schema (§20.2).
    """
    hints = []

    for rec in recs:
        # Filter to eligible types
        if rec.type not in _ELIGIBLE_TYPES:
            continue
        # Only pending
        if rec.status != "pending":
            continue
        # Must have a delivery path
        if not rec.bot_executable and rec.bridge_strategy is None:
            continue

        triggers = generate_triggers(rec)
        if not triggers:
            continue

        hint_text = generate_hint_text(rec)

        hints.append({
            "rec_id": rec.id,
            "type": rec.type,
            "priority_score": rec.priority_score,
            "triggers": triggers,
            "hint": hint_text,
        })

    return {
        "generated_at": now_iso(),
        "bot_id": bot_id,
        "hints": hints,
    }


def write_hints_for_bot(
    recs: list["Recommendation"],
    bot_id: str,
    workspace_dir: Path,
) -> None:
    """Write rec-hints.json atomically to {workspace_dir}/evolve/rec-hints.json.

    Creates parent directories if needed.
    """
    hints_dir = workspace_dir / "evolve"
    hints_dir.mkdir(parents=True, exist_ok=True)

    hints_path = hints_dir / "rec-hints.json"
    data = build_hints_file(recs, bot_id)
    text = json.dumps(data, indent=2)

    tmp = hints_path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, hints_path)

    _log.debug("write_hints_for_bot: wrote %d hints for %s", len(data["hints"]), bot_id)


def write_all_hints(
    recs: list["Recommendation"],
    network: dict,
    shared_dir: Path,
) -> None:
    """Write hint files for all bots in the network.

    For each member bot, filters recs to that bot's scope and writes
    /Users/{bot_id}/.openclaw/workspace/evolve/rec-hints.json.

    Also writes a pod-level hints file for the Evolve admin bot.
    """
    members = network.get("members", [])

    for bot_id in members:
        # Filter to recs for this specific bot
        bot_recs = [
            r for r in recs
            if r.scope_id == bot_id or (r.scope == "admin" and r.scope_id == "pod")
        ]
        workspace_dir = _bot_home(bot_id) / ".openclaw" / "workspace"
        try:
            write_hints_for_bot(bot_recs, bot_id, workspace_dir)
        except Exception as exc:
            _log.warning(
                "write_all_hints: failed to write hints for %s: %s", bot_id, exc
            )

    # Pod-level hints for the Evolve admin bot (scope_id="admin" or "pod")
    admin_recs = [
        r for r in recs
        if r.scope == "admin" or r.scope_id in ("admin", "pod")
    ]
    evolve_workspace = Path("/Users/Shared/evolve/workspace")
    try:
        write_hints_for_bot(admin_recs, "evolve", evolve_workspace)
    except Exception as exc:
        _log.debug(
            "write_all_hints: skipped evolve admin hints (workspace may not exist): %s", exc
        )
