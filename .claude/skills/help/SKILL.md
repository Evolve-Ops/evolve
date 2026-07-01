---
name: help
description: META system quick reference — link to the operator guide, the current list of aspects, and all the commands. Run anytime you forget what's available.
---

Print a tight quick-reference for the META dev system (a reminder, not a tutorial). Keep it short.

1. **Operator guide** — link `docs/using-the-meta-system.md` (the full how-to).
2. **Aspects** — list the current aspect ids by globbing
   the `meta-state/*.json` ledgers in the project memory dir (filename minus `.json`, skip
   `_README`). Each is something you can `/meta <id>`. If a ledger has a
   one-line `mission`, show it after the id. Also note the count.
3. **Commands:**
   - `/queue` — your cross-aspect inbox: what needs you. (Stale? `/reconcile` for live.)
   - `/reconcile [aspect]` — sweep live PR state now, then show the queue.
   - `/coherence` — check cross-aspect overlap / collisions / mis-routing now.
   - `/launch` — dispatch already-designed bites as chips, or list aspects to open.
   - `/prune` — archive finished/idle sessions (run from a normal/supervised session).
   - `/design "<work>"` — describe work in plain words; routes to the right aspect + opens it (the default way in — you don't pick the aspect).
   - `/triage [all]` — sweep open GitHub issues into a ranked CLOSE / DESIGN / READY / ASK digest; act by number (recommend-only; closing never automatic).
   - `/meta <aspect>` — open a coordinator directly when you already know the aspect.
   - `/status` — mid-coordinator pulse (reconcile its own chips).
   - `/close` — checkpoint a coordinator before closing it.
   - `/help` — this.
4. **Runs itself:** `meta-reconcile` (~2h — auto-merges safe PRs, relaunches stalls, pokes the red
   zone) + `meta-coherence` (daily — flags cross-aspect issues into the queue). Cadence / on-off
   live in the Scheduled sidebar.

End with: `Full how-to → docs/using-the-meta-system.md`.
