"""Deploy-time hook: register Evolve-shipped skills with OC's loader.

Phase 1.5b slice of spec §14.2. OC's skill loader scans six sources;
Evolve-shipped skills live in source 1 (``openclaw-extra``, lowest
precedence) so workspace customization always wins:

    agents.defaults.skills.load.extraDirs[]

This module's ``ensure_evo_skills_extra_dir`` runs on every primary-
bot deploy (mirrors ``ensure_evo_tools_mcp_server``'s shape). It
adds the resolved deploy-checkout path to that list — idempotent
gap-fill so a hand-edit or fresh install both end up with the same
configuration.

The skills directory itself ships at
``packages/analyzer/evolve_bot/skills/`` in the repo; the deploy
checkout location is
``/Users/Shared/evolve-repo/packages/analyzer/evolve_bot/skills/``.
We use the deploy-checkout path so every bot (including evo) reads
from the canonical deploy-managed location rather than a per-bot
copy that would drift.

Member bots are skipped — only the primary (evo) needs the skill
loader hookup. Member bots get tools via the indirect plugin path,
which is a separate surface per spec §1.3.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from platform_profile import get_profile
from .deploy_integration import (
    _bot_user_for,
    _primary_bot_id,
    _read_bot_oc_config,
    _safe_write_bot_oc_config,
)

log = logging.getLogger(__name__)


# Where Evolve-shipped skills live in the deploy checkout. Operators
# managing multiple Evolve installs may move the checkout root; the
# directory tree under it is fixed by the repo layout. Platform-keyed:
# /Users/Shared/evolve-repo on macOS, /var/lib/evolve/repo on Linux.
_DEPLOY_REPO_ROOT = get_profile().deploy_checkout_default
_SKILLS_SUBPATH = "packages/analyzer/evolve_bot/skills"


def evolve_skills_dir() -> str:
    """Return the canonical Evolve-shipped skills dir (deploy-checkout path).

    Exposed as a function so tests can monkeypatch the constant via
    ``monkeypatch.setattr`` without reaching into the module's
    namespace. Production callers should use the function not the
    constant.
    """
    return f"{_DEPLOY_REPO_ROOT}/{_SKILLS_SUBPATH}"


def _dotpath_get(obj: dict, dotted: str) -> Any:
    """Return obj at the dotted path or None. Walks dicts only; any
    non-dict mid-path returns None."""
    cur: Any = obj
    for seg in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _dotpath_set(obj: dict, dotted: str, value: Any) -> None:
    """Set obj[dotted] = value, creating intermediate dicts as needed.
    Non-dict mid-path values are overwritten (caller responsibility to
    avoid clobbering meaningful structures)."""
    cur: dict = obj
    parts = dotted.split(".")
    for seg in parts[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[parts[-1]] = value


def ensure_evo_skills_extra_dir(
    bot_id: str,
    network: dict[str, Any],
    *,
    skills_dir: str | None = None,
) -> tuple[bool, str]:
    """Ensure ``agents.defaults.skills.load.extraDirs`` contains the
    Evolve-shipped skills directory. Idempotent gap-fill — runs on
    every primary-bot deploy.

    Returns ``(changed, status)`` — same shape as
    ``ensure_evo_tools_mcp_server``:

    * ``(False, "skipped:not-primary")`` — bot_id isn't the primary bot.
      Source-1 registration is evo-only; member bots use other paths.

    * ``(False, "unchanged")`` — primary bot already has the dir in the list.

    * ``(True, "created")`` — extraDirs list was absent or didn't contain
      the dir; added.

    * ``(False, "error: <detail>")`` — read/write failed. Caller logs
      and continues; missing skill loader is non-fatal (other skill
      sources still work).

    ``skills_dir`` is exposed for tests; production callers omit it
    and the canonical deploy-checkout path is used.
    """
    primary = _primary_bot_id(network)
    if primary is None or bot_id != primary:
        log.debug(
            "ensure_evo_skills_extra_dir: skipping %s (not primary; primary=%s)",
            bot_id, primary,
        )
        return False, "skipped:not-primary"

    target_dir = skills_dir if skills_dir is not None else evolve_skills_dir()

    bot_user = _bot_user_for(bot_id, network)
    oc, err = _read_bot_oc_config(bot_id, bot_user)
    if oc is None:
        return False, f"error: read failed: {err}"

    existing = _dotpath_get(oc, "agents.defaults.skills.load.extraDirs")
    if isinstance(existing, list) and target_dir in existing:
        return False, "unchanged"

    new_cfg = deepcopy(oc)
    if not isinstance(existing, list):
        new_list: list[str] = []
    else:
        new_list = list(existing)
    new_list.append(target_dir)
    _dotpath_set(
        new_cfg, "agents.defaults.skills.load.extraDirs", new_list,
    )

    ok, msg = _safe_write_bot_oc_config(
        bot_id, new_cfg,
        reason=(
            f"evo skills loader: added Evolve-shipped skills dir to "
            f"agents.defaults.skills.load.extraDirs"
        ),
        bot_user=bot_user,
    )
    if not ok:
        return False, f"error: write failed: {msg}"

    log.info(
        "ensure_evo_skills_extra_dir: added %s to %s's extraDirs",
        target_dir, bot_id,
    )
    return True, "created"
