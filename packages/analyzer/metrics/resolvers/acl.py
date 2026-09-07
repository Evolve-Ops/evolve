"""metrics.resolvers.acl — acl.evolve_read.

1.0 if the ``evolve`` user has read access to the bot's ``.openclaw/`` dir.
0.0 otherwise.

On macOS this is checked by attempting to list the directory as the
evolve user, or by running ``/bin/ls -le`` and parsing ACL output. For
testability we default to a simple existence+readability check on the
directory from the current process; a production path running as the
evolve service user will observe the real answer. Tests override via
``set_acl_checker()``.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from metrics.registry import MetricSpec, MetricValue, register

from evolve_config import bot_home as _bot_home


_Checker = Callable[[str], tuple[bool, str]]


def _default_checker(bot_id: str) -> tuple[bool, str]:
    """Return (ok, note)."""
    path = _bot_home(bot_id) / ".openclaw"
    if not path.exists():
        return False, f"{path} does not exist"
    if not os.access(path, os.R_OK | os.X_OK):
        return False, f"no read/execute access on {path}"
    return True, f"readable: {path}"


_checker: _Checker = _default_checker


def set_acl_checker(fn: _Checker) -> None:
    """Test hook: override ACL probing."""
    global _checker
    _checker = fn


def resolve_acl_evolve_read(
    bot_id: str, as_of: datetime  # noqa: ARG001
) -> MetricValue:
    ok, note = _checker(bot_id)
    return MetricValue(
        value=1.0 if ok else 0.0,
        confidence=1.0,
        source_note=note,
    )


register(
    MetricSpec(
        name="acl.evolve_read",
        description="1.0 if the evolve user can read the bot's .openclaw/ directory.",
        unit="bool",
        source="filesystem ACL check on <bot-home>/.openclaw/",
    ),
    resolve_acl_evolve_read,
)
