"""Bug-report / feature-request intake.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §6.

On-disk layout under ``{shared_dir}/intake/``:

  open/<id>.json      — state ∈ open
  triaged/<id>.json   — admin has reviewed; awaiting external action
  filed/<id>.json     — promoted to a GitHub issue
  closed/<id>.json    — resolved / dismissed (90-day retention)
  log/<YYYY-MM-DD>.jsonl — append-only state-change log

The ``state`` field on each intake JSON is authoritative; the subdir is
the physical index. Mirror the signal-store convention so existing tooling
patterns transfer.

Public entry points:

  - :func:`store.write_intake`         — atomic write
  - :func:`store.find_intake`          — locate by id across subdirs
  - :func:`store.iter_intakes`         — yield envelopes, optional filter
  - :func:`store.transition`           — state move + file rename
  - :func:`store.new_intake_id`        — fresh id (intake-YYYYMMDD-xxxx)
"""

from . import store as store  # re-export
from .envelope import Intake, IntakeContext, IntakePromotion, IntakeKind, IntakeState

__all__ = [
    "store",
    "Intake",
    "IntakeContext",
    "IntakePromotion",
    "IntakeKind",
    "IntakeState",
]
