"""arbiter.appliers.base — Applier protocol and registry.

Each Action kind has a corresponding Applier module. Appliers:

  - ``capture_snapshot(action, bot_id) -> dict``
        called before apply; captures enough before-state to revert later
  - ``apply(action, bot_id) -> ApplyResult``
        makes the change
  - ``revert(snapshot, bot_id) -> RevertResult``
        restores to the captured before-state

The dispatch table lives at module scope and is populated by each applier
module at import time via ``register_applier()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from schema.proposal import Action


@dataclass
class ApplyResult:
    """Returned by ``Applier.apply()``.

    Fields:
        ok:        True if the apply completed successfully.
        details:   free-form applier-specific details (e.g. file path
                   written, config key patched).
        message:   human-readable summary, surfaced in the proposal
                   record's history and to the admin UI.
    """

    ok: bool
    details: dict = field(default_factory=dict)
    message: str = ""


@dataclass
class RevertResult:
    """Returned by ``Applier.revert()``.

    Fields:
        ok:       True if the revert completed successfully.
        details:  applier-specific details.
        message:  human-readable summary.
    """

    ok: bool
    details: dict = field(default_factory=dict)
    message: str = ""


class Applier(Protocol):
    """Type protocol for applier modules."""

    def capture_snapshot(self, action: Action, bot_id: str) -> dict: ...
    def apply(self, action: Action, bot_id: str) -> ApplyResult: ...
    def revert(self, snapshot: dict, bot_id: str) -> RevertResult: ...


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ─────────────────────────────────────────────────────────────────────────────

_APPLIER_REGISTRY: dict[str, Applier] = {}


def register_applier(action_kind: str, applier: Applier) -> None:
    """Register an applier for a given ``Action.kind`` tag.

    Called by each applier module at import time.
    """
    _APPLIER_REGISTRY[action_kind] = applier


def get_applier(action_kind: str) -> Applier:
    """Look up the applier for an ``Action.kind`` tag.

    Raises ``KeyError`` if no applier is registered — the caller should
    translate that into a meaningful error at the arbiter level.
    """
    if action_kind not in _APPLIER_REGISTRY:
        raise KeyError(
            f"No applier registered for action kind {action_kind!r}. "
            "Either the module wasn't imported, or this action kind is "
            "from a future layer that hasn't shipped yet."
        )
    return _APPLIER_REGISTRY[action_kind]


def known_action_kinds() -> list[str]:
    """Return the sorted list of action kinds with registered appliers."""
    return sorted(_APPLIER_REGISTRY.keys())
