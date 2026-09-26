"""observations — Query helper for the observation stream (L1).

L1 ships reads against the existing JSONL annotation files produced by
the plugin's session summarizer. Tuple extraction arrives in L3; until
then, ``ObservationWindow.tuples()`` and ``.clusters()`` raise
``NotImplementedError`` with clear guidance.

See docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §6.
"""

from observations.access import (
    ObservationWindow,
    TupleAccessNotYetAvailable,
    window,
)

__all__ = [
    "ObservationWindow",
    "TupleAccessNotYetAvailable",
    "window",
]
