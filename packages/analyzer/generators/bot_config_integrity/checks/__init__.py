"""Registry of bot_config_integrity checks.

Adding a new check:
  1. Drop ``<name>.py`` in this directory with a module-level
     ``run(ctx, cfg) -> list[Proposal]`` function.
  2. Import it here and add it to ``CHECKS``.

The dispatcher in ``observe.py`` iterates ``CHECKS`` once per bot per
run. Each check is independent; a check's failure doesn't block other
checks (the dispatcher catches+swallows per-check exceptions).
"""

from generators.bot_config_integrity.checks import (
    catalog_provider_coverage,
    catalog_tier_drift,
)


# Explicit list, ordered for stable Proposals queue output. Add new
# checks at the bottom so existing operator workflows aren't disturbed
# by a reordering.
CHECKS = [
    catalog_tier_drift,
    catalog_provider_coverage,
]


__all__ = ["CHECKS"]
