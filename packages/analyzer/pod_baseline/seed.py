"""pod_baseline.seed — propose a baseline from the pod's current modal values.

Pure: takes BotReadings, returns a PodBaseline plus a per-surface
explanation of what was chosen and why (the CLI prints it — seeding is
never silent). Unreadable bots are excluded from the vote and reported as
such; a surface with no readable value at all seeds ``"unset"``.

Ties break to the lexicographically smallest value, flagged in the
explanation so the operator knows the choice was arbitrary and can edit
the file (it is operator-editable by design).

Relation to ``permissions.bootstrap._modal_value`` (the permission-baseline
seeder): same modal idea, deliberately different semantics — that seeder
lets ``None`` (field unset) vote and breaks ties by first occurrence; this
one excludes unreadable bots from the vote (``"unset"`` is a real value
that votes; ``None`` means the config couldn't be read) and needs the full
per-value counts + a tie flag for the printed explanation, so the vote core
is not shareable as-is. If vote semantics ever change, check both.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pod_baseline.schema import SURFACES, PodBaseline


@dataclass
class SeedChoice:
    surface: str
    chosen: str
    counts: dict  # value -> number of bots observed at it
    tie: bool
    unknown: int  # bots whose value was unreadable


def seed_from_majority(readings: list, *, generated_at: str) -> tuple:
    """Compute (PodBaseline, list[SeedChoice]) from the pod's modal values.

    Seeds no exceptions — minority bots will census as DRIFT until the
    operator declares them, which is the point: deviation becomes the
    surfaced object, and silence is never consent.
    """
    surfaces: dict = {}
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
        if counts:
            top = max(counts.values())
            winners = sorted(v for v, c in counts.items() if c == top)
            chosen = winners[0]
            tie = len(winners) > 1
        else:
            chosen = "unset"
            tie = False
        surfaces[surface] = chosen
        choices.append(
            SeedChoice(surface=surface, chosen=chosen, counts=dict(counts), tie=tie, unknown=unknown)
        )
    baseline = PodBaseline(updated_at=generated_at, surfaces=surfaces, exceptions=[])
    return baseline, choices
