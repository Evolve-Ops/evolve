"""Better-engine-config writers for a bot's cost defaults.

Tiny wrappers around the canonical ``better_engine_config`` setters, factored
out of ``deploy.py`` (a frozen no-growth file). Both keep the analyzer import
lazy — ``deploy.py`` is imported very early in the wizard, before the analyzer
package is guaranteed importable.

  - ``set_bot_created_at`` stamps ``bots.<bot>.created_at`` so the resolver can
    age-grade the graduated new-bot daily-cap default (see
    ``better_engine_config.budget_hard_cap_usd`` / ``NEW_BOT_DAILY_HARD_USD``).
  - ``set_per_bot_daily_hard`` writes an explicit per-bot daily hard cap
    override (wins over the graduated default and the pod default).

Both load + atomically save the whole config; callers persist nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


def set_bot_created_at(shared_dir: Path, bot_id: str, when: "datetime") -> None:
    """Stamp ``bots.<bot>.created_at`` (drives the graduated new-bot cap)."""
    from better_engine_config import load as _load_be, save as _save_be

    be = _load_be(shared_dir)
    be.set_bot_created_at(bot_id, when)
    _save_be(be, shared_dir)


def set_per_bot_daily_hard(shared_dir: Path, bot_id: str, cap_usd: float) -> None:
    """Write an explicit per-bot daily hard cap override."""
    from better_engine_config import load as _load_be, save as _save_be

    be = _load_be(shared_dir)
    be.set_per_bot_daily_hard_usd(bot_id, cap_usd)
    _save_be(be, shared_dir)
