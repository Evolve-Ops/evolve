"""google_tools_policy.py — expose a bot's Google tools past a curated tool profile.

The Evolve plugin registers the curated Google tools (gmail_*/drive_*/
calendar_*) at runtime for any bot with a configured ``google_integration``.
But a bot whose ``tools.profile`` is a curated baseline (e.g. ``"coding"``) has
those plugin-registered tools FILTERED OUT of the agent's executable toolset —
the agent then calls ``gmail_list_messages`` and gets "Tool gmail_list_messages
not found" (confirmed live 2026-06-22: profile-less bots dispatch Google fine;
``coding``-profile bots get 100% not-found). OC's ``tools.alsoAllow`` is the
documented "merged on top of the selected tool profile" additions list (NOT
``tools.allow``, which REPLACES the profile and would strip every other tool —
``docs/schemas/oc-config-schema.txt`` §tools). Adding the Google tool names
there re-exposes them past whatever profile the bot runs.

This is the tool-POLICY half of making a bot use Google; the capability-
AWARENESS half (the agent KNOWS to call the tool) is the per-turn capability
block. Both are needed end to end.

Lives in its own module (not deploy.py) so the deploy hot-file stays under its
no-growth cap, and so the Google-enable wizard paths can reuse the same logic.
"""

from __future__ import annotations


def ensure_google_tools_allowlisted(cfg: dict, bot_id: str, network: dict) -> bool:
    """Ensure a Google-configured bot's ``tools.alsoAllow`` carries the Google tools.

    Mutates ``cfg`` (the bot's openclaw.json dict) in place. Returns True iff an
    entry was added.

    Gated on ``google_integration`` (NOT on ``profile == "coding"``): Evolve
    doesn't set ``tools.profile`` (operators/OC do), and the addition is a
    harmless no-op for a profile-less bot that already has the tools. Sourced
    from ``google_service.TOOL_SPECS`` so the list can't drift from what the
    plugin actually registers. Union with any existing ``alsoAllow`` entries —
    never clobbers operator-set additions. Soft-fails to a no-op on any error so
    a google_service import/quirk never aborts a deploy.
    """
    try:
        from . import google_service
        if not google_service.is_google_configured(bot_id, network):
            return False
        names = sorted(google_service.TOOL_SPECS.keys())
    except Exception:  # noqa: BLE001 — never block a deploy on this enrichment
        return False
    if not names:
        return False
    tools_section = cfg.setdefault("tools", {})
    existing = tools_section.get("alsoAllow")
    if not isinstance(existing, list):
        existing = []
    have = {x for x in existing if isinstance(x, str)}
    missing = [n for n in names if n not in have]
    if not missing:
        return False
    tools_section["alsoAllow"] = existing + missing
    return True
