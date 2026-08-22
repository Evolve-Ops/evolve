"""retired_jobs.py — the per-bot jobs Evolve no longer installs, and the
pod-wide sweep that tears them off pods that still carry them.

**Why this is its own module rather than a helper inside ``deploy.py``.**
Teardown used to have exactly one trigger: ``deploy_bot`` step 6
(``deploy._bootout_retired_per_bot_jobs``). That made it depend on a bot
being *redeployed*, and on a pod with no manual deploys the only thing that
redeploys a bot is the repo-puller's lagging-bot sweep
(``repo_puller._run_lagging_bot_redeploy_sweep``). That sweep is skipped on
every HEAD-advancing tick by design — the ``deploy`` module the tick process
imported is the PRE-pull commit, so stamping from it would mark the fleet
current at a superseded version (``repo_puller._loaded_deploy_code_is_current``).
It therefore only converges on a **no-op** tick.

On 2026-08-18 that assumption broke on both production pods. #3705 retired
``ai.openclaw.evolve.apply.<bot>`` and deleted the script its units named.
The commit landed inside a run of six consecutive advancing puller ticks —
merges kept arriving inside every 15-minute window, so there was no no-op
tick — and the sweep was skipped every single time. Nine launchd plists on
the macOS pod and two systemd units on the Linux pod kept firing on their
interval and exiting non-zero against a deleted file, indefinitely, until an
operator ran ``evolve-admin deploy --all`` by hand.

So teardown gets a second trigger that shares none of that machinery:
:func:`sweep_pod` is pod-wide, stamp-free, and version-blind. The puller
calls it on **every** tick — advance or no-op — outside the freshness guard
and outside the deploy lock, because removing a label nothing installs
anymore can neither race an install nor write a version stamp. Worst-case
exposure is one tick: the tick that pulls a retirement is still running
pre-pull code that does not know the label is retired, and the next tick's
fresh process does. That bound holds regardless of merge traffic, which is
the property the old path lacked.

``deploy_bot``'s per-bot sweep stays. The two overlap deliberately: this one
is cheap and frequent but gated on the artifact being on disk, while the
deploy-time one calls ``remove()`` unconditionally and so also catches the
file-gone-but-still-loaded residue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Label templates for every per-bot job Evolve has retired. Formatted with
# ``bot=<bot_id>``. SOURCE OF TRUTH — ``deploy._bootout_retired_per_bot_jobs``
# and :func:`sweep_pod` both read this list, so the two teardown paths cannot
# drift apart.
#
# * ``ai.openclaw.evolve.test.<bot>``  — killed 2026-06-08 with the app-test
#   surface (docs/decision-app-tests-2026-06-08.md).
# * ``ai.openclaw.evolve.apply.<bot>`` — killed 2026-08-18 with the dead
#   per-bot apply watcher (docs/design-proposal-signing-key-2026-08-18.md).
RETIRED_PER_BOT_LABEL_TEMPLATES: tuple[str, ...] = (
    "ai.openclaw.evolve.test.{bot}",
    "ai.openclaw.evolve.apply.{bot}",
)


def retired_labels_for(bot_id: str) -> list[str]:
    """Every retired per-bot label for ``bot_id``."""
    return [t.format(bot=bot_id) for t in RETIRED_PER_BOT_LABEL_TEMPLATES]


def _pod_bot_ids(shared_dir: Path) -> list[str]:
    """Every bot id the pod ledger knows, from ``install.json``.

    Union of ``bots`` (the roster) and the ``bot_versions`` keys (the deploy
    stamps). The union rather than either alone: a bot removed from the
    roster can still have left units on disk, and that residue is exactly
    what this sweep is for. Empty list when install.json is missing or
    unreadable — a pod with no ledger has nothing to sweep against.
    """
    try:
        info = json.loads((shared_dir / "install.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(info, dict):
        return []
    ids: list[str] = []
    for b in info.get("bots") or []:
        if isinstance(b, str) and b not in ids:
            ids.append(b)
    for b in (info.get("bot_versions") or {}):
        if isinstance(b, str) and b not in ids:
            ids.append(b)
    return ids


def sweep_pod(
    shared_dir: Path, *, sched: Any = None,
) -> tuple[list[str], dict[str, str]]:
    """Remove every retired per-bot job still installed on this pod.

    Returns ``(removed_labels, errors)``; ``errors`` maps label → message.
    Never raises — a teardown failure must not break the caller's tick.

    Cheap in the steady state, which is what lets it run every 15 minutes:
    the on-disk artifact path is a pure path computation on both adapters
    (``artifact_path`` — the plist on macOS, the ``.service`` unit on Linux),
    so a pod with nothing retired installed spends N×len(templates)
    ``Path.exists()`` calls and spawns no subprocess at all. ``remove()`` —
    which does the bootout/``disable --now`` and is not cheap — is reached
    only when an artifact is actually there.

    That gate is why ``deploy_bot``'s per-bot sweep is still needed: it calls
    ``remove()`` unconditionally and so also clears a unit that is still
    registered with launchd/systemd after its file was removed by hand.
    """
    if sched is None:
        from runtime.scheduler import get_scheduler
        sched = get_scheduler()

    removed: list[str] = []
    errors: dict[str, str] = {}
    for bot_id in _pod_bot_ids(shared_dir):
        for label in retired_labels_for(bot_id):
            try:
                if not Path(sched.artifact_path(label)).exists():
                    continue
                ok, msg = sched.remove(label)
            except Exception as exc:   # never let a teardown break a tick
                errors[label] = f"{type(exc).__name__}: {exc}"
                continue
            if ok:
                removed.append(label)
            else:
                errors[label] = msg
    return removed, errors
