"""list_proposals — summarize Proposals from the arbiter store.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §5.1.
Backing store: ``analyzer.arbiter.store`` (CLAUDE.md "Arbiter on-disk
layout"). Proposals live under ``{shared_dir}/proposals/<subdir>/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any



DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# Subdir routing per the arbiter store. The ``state`` filter the LLM
# passes maps to one or more subdirs; we don't expose subdir names
# directly because they're an implementation detail.
_STATE_TO_SUBDIRS: dict[str, tuple[str, ...]] = {
    "pending":   ("pending",),
    "snoozed":   ("snoozed",),
    "applied":   ("applied",),
    "archived":  ("archived",),
    "active":    ("pending", "snoozed"),  # actionable, not yet decided
    "all":       ("pending", "snoozed", "applied", "archived"),
}


def list_proposals(
    shared_dir: Path,
    *,
    state: str | None = "pending",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return Proposal summaries for the requested state.

    Returns ``{"proposals": [{...}, ...], "count": N, "filters": {...}}``.
    Each summary is a small subset of the full Proposal envelope:
    ``{id, title, status, generator_id, bot_id, motivating_signals,
    created_at}``. The bot can call /api/proposals/<id> to get the full
    envelope if it needs more.
    """
    from arbiter import store as _store  # type: ignore[import-not-found]

    norm_state = state if state in _STATE_TO_SUBDIRS else "pending"
    subdirs = _STATE_TO_SUBDIRS[norm_state]
    capped_limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))

    summaries: list[dict[str, Any]] = []
    for prop in _store.iter_proposals(shared_dir, subdirs=subdirs):
        summaries.append(_summarize(prop))
        if len(summaries) >= capped_limit:
            break

    return {
        "proposals": summaries,
        "count": len(summaries),
        "filters": {
            "state": norm_state,
            "limit": capped_limit,
        },
    }


def _summarize(proposal: Any) -> dict[str, Any]:
    """Trim a full Proposal envelope to the fields the bot needs.

    The full Proposal can be tens of KB with diffs / verifications /
    history — too much for an LLM tool call when the bot just wants
    "what's in the queue?" Return the smallest envelope that lets the
    bot describe the proposal to admin and follow up via
    /api/proposals/<id> if needed.

    The schema has no "title" field; ``admin_surface_summary`` is the
    human-readable headline the admin UI uses, with ``problem`` as the
    fallback when a generator didn't populate the summary.
    """
    title = (
        (getattr(proposal, "admin_surface_summary", "") or "").strip()
        or (getattr(proposal, "problem", "") or "").strip().splitlines()[0:1]
    )
    if isinstance(title, list):
        title = title[0] if title else ""
    return {
        "id": proposal.id,
        "title": title,
        "status": getattr(proposal, "status", "") or "",
        "generator_id": getattr(proposal, "generator_id", "") or "",
        "dimension": getattr(proposal, "dimension", "") or "",
        "bot_id": getattr(proposal, "bot_id", None),
        "motivating_signals": list(getattr(proposal, "motivating_signals", []) or []),
        "created_at": getattr(proposal, "created_at", "") or "",
    }
