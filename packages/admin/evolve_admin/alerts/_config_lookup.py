"""Defensive config lookup for the alert dispatcher.

The dispatcher reads ``alerts.*`` keys from ``better-engine-config.json``
via ``config_sandbox.resolve``. The sandbox raises ``KeyError`` for paths
that don't exist in the schema — which is the case in Phase 1 (the
schema entries land in Phase 2). This module wraps the call and falls
back to a caller-supplied default for any failure mode:

  - schema entry not yet declared (KeyError) → default
  - config file missing or corrupt → default
  - any other resolve() exception → default

Once Phase 2 lands the schema entries, ``lookup`` quietly starts
returning the operator-tunable values without any code change in the
dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def lookup(shared_dir: Path, path: str, default: Any) -> Any:
    """Resolve a config_sandbox key, falling back to ``default`` on any error."""
    try:
        from evolve_admin.config_sandbox import resolve  # type: ignore[import-untyped]
    except Exception:
        return default

    try:
        result = resolve(path, shared_dir=shared_dir)
    except KeyError:
        return default
    except Exception:
        return default

    if result is None:
        return default
    value = getattr(result, "value", None)
    return default if value is None else value
