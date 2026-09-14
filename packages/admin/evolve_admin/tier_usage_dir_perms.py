"""Multi-writer perms for ``{sharedDir}/cost/tier-usage/`` — the role-cap ledger.

``deploy.ensure_pod_perms`` imports :func:`check_tier_usage_dir`; tests import
it and the constants directly.

THE BUG (found 2026-09-04). The plugin's
``ModelRouter._appendTierUsageRecord`` runs as the BOT user and appends one
JSONL record to ``{sharedDir}/cost/tier-usage/{botId}/{date}.jsonl`` every
time a session transitions into the ``power`` or ``max`` role. That ledger is
the DURABLE half of the per-role daily cap: ``_seedRoleCountersFromDisk``
reads it at construction so a gateway restart does not hand every bot a fresh
budget.

On the reference pod the tree was ``drwxr-xr-x evo:wheel`` — created by
whichever writer won the mkdir race, which was the ``evo`` user in June — so
every bot except the pod's own ``evolve`` bot got EACCES on the append. The
write is wrapped in a no-throw try, and the warn inside it called a
``logger`` property ``ModelRouter`` never had, so nothing surfaced: the caps
looked enforced, seeded 0 on every gateway start, and in practice bounded
nothing across restarts.

THE FIX. Same contract the other multi-writer pod dirs already carry
(``proposals/``, ``alerts/`` — see ``deploy._check_alerts_dir``): mode 1777,
owner ``evolve``.

* **1777** — every bot daemon writes here as its own user, so ownership must
  not decide write access. The sticky bit is what keeps one bot from deleting
  another bot's ledger.
* **owner evolve** — asserted separately for the reason the alerts dir
  documents: whoever wins the mkdir race on a fresh install silently becomes
  the owner, writes keep working, and no daemon notices until ``evolve``
  needs to repair or prune the tree. Pairing the mode check with an owner
  check is what makes that drift visible.

Both levels are checked. ``cost/`` is a plain parent today, but a bot that
has never used a capped role must still be able to create
``tier-usage/{botId}/`` on its first transition, and that needs write on the
parent chain.

The per-bot ``{botId}/`` leaf underneath is deliberately NOT enforced here:
it is bot-owned by design (the same shape as ``annotations/{botId}/``), and
1777 on its parent is exactly what lets each bot mint its own.
"""

from __future__ import annotations

from pathlib import Path

# Sticky world-writable, as for proposals/ and alerts/: multi-writer by
# construction (one appender per bot daemon, each running as its own user).
TIER_USAGE_DIR_MODE = 0o1777

# Relative to {sharedDir}, outermost first — each level needs the mode for the
# next one down to be creatable by a bot user.
TIER_USAGE_DIRS: tuple[str, ...] = ("cost", "cost/tier-usage")


def check_tier_usage_dir(shared_dir: "str | Path") -> list:
    """One ``_PermCheck`` pair (mode + owner) per level of the ledger tree.

    Creates missing levels (``create=True``) so a pod that has never had a
    capped-role transition still gets the contract, rather than waiting for
    the first bot to race for the mkdir and win it with the wrong perms.

    Returns ``deploy._PermCheck`` instances (imported lazily to avoid the
    import cycle — deploy.py imports this module at load time).
    """
    from .deploy import _check_dir_mode, _check_dir_owner  # lazy: cycle

    root = Path(shared_dir)
    checks: list = []
    for rel in TIER_USAGE_DIRS:
        path = root / rel
        checks.append(_check_dir_mode(path, TIER_USAGE_DIR_MODE, create=True))
        checks.append(_check_dir_owner(path, "evolve"))
    return checks
