"""bot_purpose — the per-bot purpose/archetype anchor (Effectiveness Layer, Phase B).

The Fit Reviewer (Layer 2) needs to know *what a bot is for* to judge what "more
effective" even means for it. That anchor is stored at
``network.json::bots[<id>].purpose``. The operator declares it through the write
API (``PUT /api/bot/<id>/purpose``), surfaced in two places: the optional,
skippable purpose step in the bot-creation wizard, and the Purpose card on the
per-bot Settings page. When the wizard step is skipped, the per-bot Setup
checklist's ``purpose`` item stays pending and nags until it's filled in. The
in-bot reviewer reads the stored anchor. See
internal/spec-effectiveness-layer-2026-06-09.md §4.

This module is the single source of truth for the archetype vocabulary, validation,
and reads — importable from both the analyzer (the reviewer) and the admin (the
write API), so the two never drift.
"""

from __future__ import annotations

from typing import Any

# The starting archetype set. Each drives a Fit-Reviewer playbook (the lenses it
# applies). Extensible — add an archetype here and give it a playbook. "custom"
# is the catch-all: a bot with a freeform mission but no canonical archetype.
ARCHETYPES: tuple[str, ...] = (
    "personal-assistant",
    "project-manager",
    "home-automation",
    "research-analyst",
    "customer-facing",
    "custom",
)

CAPTURED_MODES: tuple[str, ...] = ("declared", "inferred")

# A mission is one human line, not a paragraph — keep it tight.
MISSION_MAX_CHARS = 280


def normalize_purpose(raw: dict[str, Any] | None, *, captured: str = "declared") -> dict[str, Any] | None:
    """Validate + canonicalize a purpose payload, or return None if empty.

    - ``archetype`` is lowercased; anything outside ARCHETYPES becomes ``custom``.
    - ``mission`` is trimmed to ``MISSION_MAX_CHARS``.
    - ``captured`` is ``declared`` (operator/wizard) or ``inferred`` (the reviewer);
      ``confidence`` is forced to 1.0 for declared purposes, kept for inferred ones.
    - ``reviewed_at`` is preserved if present (the write path stamps it).
    """
    if not raw:
        return None
    archetype = str(raw.get("archetype") or "").strip().lower()
    mission = str(raw.get("mission") or "").strip()[:MISSION_MAX_CHARS]
    if not archetype and not mission:
        return None
    if archetype not in ARCHETYPES:
        archetype = "custom"
    captured = captured if captured in CAPTURED_MODES else "declared"
    out: dict[str, Any] = {
        "archetype": archetype,
        "mission": mission,
        "captured": captured,
        "confidence": 1.0 if captured == "declared" else _clamp01(raw.get("confidence", 1.0)),
    }
    if raw.get("reviewed_at"):
        out["reviewed_at"] = str(raw["reviewed_at"])
    return out


def set_bot_purpose(
    network: dict[str, Any],
    bot_id: str,
    raw: dict[str, Any] | None,
    *,
    captured: str = "declared",
    reviewed_at: str | None = None,
) -> dict[str, Any] | None:
    """Declare (or clear) a bot's purpose in a loaded network config, in place.

    The single home for the purpose *write* contract — the admin endpoint
    (``PUT /api/bot/<id>/purpose``) and the Fit-Reviewer targeting CLI both go
    through here so they never drift. Normalizes the payload, stamps
    ``reviewed_at`` on a non-empty purpose, and writes it under
    ``bots[bot_id].purpose``. An empty/blank payload clears the purpose.

    ``reviewed_at`` is passed in (not stamped here) so this stays clock-free and
    unit-testable; callers pass ``datetime.now(...).isoformat()``.

    Returns the stored purpose dict, or None when cleared. Raises ``KeyError``
    if ``bot_id`` is not already a member — pod membership is ``network.json``'s
    source of truth, never something a purpose write invents.
    """
    bots = network.get("bots")
    if not isinstance(bots, dict) or not isinstance(bots.get(bot_id), dict):
        raise KeyError(f"bot {bot_id!r} is not a member in network.bots")
    purpose = normalize_purpose(raw, captured=captured)
    if purpose is None:
        bots[bot_id].pop("purpose", None)
        return None
    if reviewed_at:
        purpose["reviewed_at"] = str(reviewed_at)
    bots[bot_id]["purpose"] = purpose
    return purpose


def get_bot_purpose(network: dict[str, Any], bot_id: str) -> dict[str, Any] | None:
    """Return the bot's purpose dict from a loaded network config, or None."""
    return ((network.get("bots") or {}).get(bot_id) or {}).get("purpose")


def has_purpose(network: dict[str, Any], bot_id: str) -> bool:
    """True if the bot has a declared/inferred purpose with at least an archetype."""
    p = get_bot_purpose(network, bot_id)
    return bool(p and p.get("archetype"))


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, f))
