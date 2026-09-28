"""openclaw.json keys Evolve itself used to write that the installed
OpenClaw now rejects outright — the writer-side half of
breaker-notify-survives-openclaw-config-validation.

Kept out of ``deploy.py`` (a frozen, no-growth-capped file — see
``tools/file-size-ratchet``) so both ``deploy.py`` and ``heal.py`` can share
one small module instead of one importing the other's monolith.

Each entry is a strict-schema ``Unrecognized key`` under 2026.9.2, not a
rename — OpenClaw dropped the knob entirely (canonical built-in defaults now
apply). Deliberately narrow: this is a targeted removal of keys THIS
codebase put there, never a ``doctor --fix``-shaped sweep of every legacy
key OpenClaw itself may have written (that repair is deliberate-proposal
territory — see ``config-stated-vs-live-and-write-refusal-alert``).
"""

from __future__ import annotations

from typing import Any

# agents.defaults.contextPruning.keepLastAssistants — written by
# deploy._BALANCED_COST_DEFAULTS and cost_profiles.BUILTIN_PROFILES
# (conservative/balanced); OC 2026.9.2 retired it with no replacement.
RETIRED_EVOLVE_WRITTEN_KEYS: tuple[tuple[str, ...], ...] = (
    ("agents", "defaults", "contextPruning", "keepLastAssistants"),
    # agents.defaults.compaction.reserveTokensFloor — written by the same two
    # writers; 2026.9.2 fixed the reserve at 20k. Its replacement as a budget
    # is compaction.maxActiveTranscriptBytes (compaction_housekeeping.py).
    ("agents", "defaults", "compaction", "reserveTokensFloor"),
)


def strip_retired_openclaw_keys(cfg: dict) -> bool:
    """Remove keys Evolve used to write that OpenClaw now rejects.

    Gap-fill (``deploy.gap_fill_cost_settings``) only ever ADDS a missing
    key, so a bot deployed before this fix keeps carrying the retired key
    forever unless something actively removes it. This is that subtraction
    — called from both ``deploy.ensure_plugin_config`` (every deploy) and
    ``heal.check_pod_conduct_injection`` (every heal cycle, via the drift-
    apply write path — staging + ``sudo /bin/cp`` + chown/chmod, per
    CLAUDE.md "Writes") — so an already-deployed bot, including the
    ``evolve`` service account's own bot entry, sheds the stale key on
    whichever of the two runs next.

    Returns True iff anything was removed.
    """
    changed = False
    for path in RETIRED_EVOLVE_WRITTEN_KEYS:
        node: Any = cfg
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict) and path[-1] in node:
            del node[path[-1]]
            changed = True
    return changed
