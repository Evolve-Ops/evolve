"""Bot lifecycle management — discovery and the three retirement paths.

Three first-class lifecycle paths for retiring a bot from a pod:

  1. **Detach** (today: ``evolve-admin remove-evolve``) — strip Evolve
     from a bot but leave it running as an independent OpenClaw bot.
     Keeps gateway, openclaw.json (minus evolve block), workspace,
     crons, channels, and macOS user. Removes Evolve plugin,
     per-bot evolve launchd daemons, and marks the bot
     ``evolve_disabled=true`` in network.json.

  2. **Archive** (today: ``evolve-admin retire-bot``) — graceful full
     retirement. Stops everything (gateway + per-bot daemons), archives
     the workspace + closure summary under ``{shared_dir}/archive/``,
     removes from network.json. macOS user stays. Reversible via
     archive restore + redeploy.

  3. **Delete** (not yet implemented) — irreversible full removal.
     Archive's actions + delete macOS user account + generate a
     manual-cleanup checklist for off-host artifacts (backup repo,
     bot tokens, SSH deploy keys, etc.) that Evolve cannot safely
     automate from inside the pod.

The :mod:`inventory` submodule is the shared discovery layer. Pure
read-only — walks every place artifacts can hide for a given bot,
classifies each by which lifecycle action(s) affect it, and returns
a structured :class:`BotInventory`. Used by all three operations to
power their dry-run preview and the future Bot Lifecycle UI page.
"""

from .inventory import (
    BotInventory,
    InventoryItem,
    ItemCategory,
    LifecycleAction,
    compile_bot_inventory,
)

__all__ = [
    "BotInventory",
    "InventoryItem",
    "ItemCategory",
    "LifecycleAction",
    "compile_bot_inventory",
]
