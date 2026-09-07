"""pod_baseline.seed — propose a baseline from the pod's current readings.

Pure: takes BotReadings, returns a PodBaseline plus a per-surface
explanation of what was chosen and why (the CLI prints it — seeding is
never silent). Unreadable bots are excluded from the vote and reported as
such.

Q7(b) (decided 2026-08-22) put two rules on top of the plain modal vote:

- **A no-intent sentinel is never elected.** If ``"custom"`` or ``"unset"``
  is among the modal readings, the surface is seeded **undeclared** rather
  than being given a value. ``custom`` is not a position on a safety axis —
  it is the absence of a declaration, and there is no "safest" custom;
  ``unset`` is the same fact spelled as an absent knob, and arguably worse,
  because it means *upstream's* default governs and can move under the fleet
  with an OC release. Electing either by any rule elects a non-answer, and
  the census then reports a fleet's silence back to it as conformance.

  A *tied* sentinel blocks election too, not just an outright winner: if as
  many bots declared nothing as declared the leading value, then the leading
  value is not the pod's position either.
- **"Safest observed" is the tiebreak**, and only among values that are
  really values. Where the surface has a safety ordering
  (``pod_baseline.ordering``) and one tied winner is strictly safer than
  every other, that one wins. Where the ordering cannot decide — no ordering
  on the surface, or mutually incomparable winners like ``coding`` vs
  ``messaging`` — the tiebreak falls back to the lexicographically smallest
  value, and :attr:`SeedChoice.tie_broken_by` says so, so the operator knows
  the choice was arbitrary and can edit the file (it is operator-editable by
  design).

Relation to ``permissions.bootstrap._modal_value`` (the permission-baseline
seeder): same modal idea, deliberately different semantics — that seeder
lets ``None`` (field unset) vote and breaks ties by first occurrence; this
one excludes unreadable bots from the vote (``"unset"`` votes, and votes to
leave the surface undeclared; ``None`` means the config couldn't be read),
refuses sentinel winners outright, and needs the full per-value counts + a
tiebreak note for the printed explanation, so the vote core is not shareable
as-is. If vote semantics ever change, check both.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pod_baseline import ordering
from pod_baseline.schema import NO_INTENT_SENTINELS, SURFACES, PodBaseline

TIEBREAK_NONE = ""
TIEBREAK_SAFEST = "safest"
TIEBREAK_LEXICOGRAPHIC = "lexicographic"


@dataclass
class SeedChoice:
    surface: str
    chosen: str  # "" when the surface is seeded undeclared
    counts: dict  # value -> number of bots observed at it
    tie: bool
    unknown: int  # bots whose value was unreadable
    undeclared: bool = False  # seeded with no declared intent (Q7(b))
    blocking_sentinels: tuple = ()  # the sentinel readings that blocked election
    tie_broken_by: str = TIEBREAK_NONE  # "" | "safest" | "lexicographic"


def seed_from_majority(readings: list, *, generated_at: str) -> tuple:
    """Compute (PodBaseline, list[SeedChoice]) from the pod's readings.

    Seeds no exceptions — minority bots census as a drift state until the
    operator declares them, which is the point: deviation becomes the
    surfaced object, and silence is never consent.
    """
    surfaces: dict = {}
    undeclared: list = []
    choices: list = []
    for surface in SURFACES:
        counts: Counter = Counter()
        unknown = 0
        for reading in readings:
            sr = reading.surfaces.get(surface)
            if sr is None or sr.value is None:
                unknown += 1
            else:
                counts[sr.value] += 1

        if not counts:
            # Nothing readable at all: there is no vote to hold, and an
            # empty pod has declared nothing. Undeclared, not "unset".
            choices.append(SeedChoice(
                surface=surface, chosen="", counts={}, tie=False, unknown=unknown,
                undeclared=True,
            ))
            undeclared.append(surface)
            continue

        top = max(counts.values())
        winners = sorted(v for v, c in counts.items() if c == top)
        tie = len(winners) > 1
        sentinels = tuple(v for v in winners if v in NO_INTENT_SENTINELS)
        if sentinels:
            choices.append(SeedChoice(
                surface=surface, chosen="", counts=dict(counts), tie=tie,
                unknown=unknown, undeclared=True, blocking_sentinels=sentinels,
            ))
            undeclared.append(surface)
            continue

        if not tie:
            chosen, broken_by = winners[0], TIEBREAK_NONE
        else:
            safest = ordering.safest(surface, winners)
            if safest is not None:
                chosen, broken_by = safest, TIEBREAK_SAFEST
            else:
                chosen, broken_by = winners[0], TIEBREAK_LEXICOGRAPHIC
        surfaces[surface] = chosen
        choices.append(SeedChoice(
            surface=surface, chosen=chosen, counts=dict(counts), tie=tie,
            unknown=unknown, tie_broken_by=broken_by,
        ))

    baseline = PodBaseline(
        updated_at=generated_at,
        surfaces=surfaces,
        undeclared=undeclared,
        exceptions=[],
    )
    return baseline, choices
