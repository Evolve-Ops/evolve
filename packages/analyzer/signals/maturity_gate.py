"""signals.maturity_gate — fresh-pod "no-data-yet" gate for history-dependent
producers.

Spec: docs/spec-transient-signal-suppression-2026-06-23.md (Mechanism 3).

The third fresh-pod alert-protection mechanism, sibling to
:mod:`signals.settle_gate` (bring-up settle window) and
:mod:`signals.flap_gate` (self-healing flap hysteresis).

Those two quiet *transient* conditions. This one quiets a different failure
mode: a producer whose signal/summary is computed from **accumulated history**
pages a scary state on a brand-new pod purely *because there is no history
yet*. The canonical case is the RSI Weekly Review on a 1-day-old pod —
``Approval Rate 0/100``, ``Measurement Rate 0/100`` → ``🔴 At Risk`` — when the
honest reading is "warming up", not "at risk".

The gate is a **predicate consulted at the producer's emission point** (same
shape as the other two gates). It never hand-edits Signal/summary JSON; the
producer asks ``is this pod mature enough for my output to be meaningful?`` and,
if not, reframes its own output to an informational "warming up / insufficient
data" state (or withholds it).

Two dimensions, each optional; a producer declares whichever it depends on:

  * **pod age** — time since first bring-up, read from the *same* marker the
    settle gate established (``settle_gate.pod_age``). Reusing that anchor means
    "pod age" has one definition across all three mechanisms.
  * **data-point count** — a producer-supplied count of the accumulated records
    its output needs (e.g. proposals generated in the funnel window). The gate
    does not gather this — the producer passes the number it already computed.

A producer is **mature** only when *every declared* dimension is satisfied
(age-old-enough AND data-rich-enough). Equivalently it is "warming up" when
*either* dimension is short — the conservative direction, since a low score
born of no history is exactly what we must not page.

Fail-OPEN to mature: any uncertainty (undeterminable pod age, a dimension the
producer left unspecified) is treated as satisfied. The gate must never
silently suppress a genuine, fully-grounded finding — when in doubt, show the
real content.

Leaf module: imports only stdlib + the sibling :mod:`signals.settle_gate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import settle_gate


# ── Default thresholds for the RSI Weekly Review (summaries.weekly_rsi_review) ──
# The review compares "this week vs the 4-week average" and scores an approval /
# measurement funnel — none of which is meaningful before the pod has lived
# through at least one prior weekly cycle. 14 days = two full ISO weeks: enough
# for a real week-over-week comparison and a non-trivial funnel denominator,
# without delaying the first genuine review by a whole month.
RSI_REVIEW_MIN_AGE_DAYS = 14.0
# …and at least one proposal must have entered the funnel window, so the
# approval/measurement rates have a real denominator. A pod that has lived
# 14 days yet produced literally zero proposals is "no data to score", not
# "at risk" — the genuine "RSI went silent" concern is a separate
# producer-liveness signal, not this summary's job.
RSI_REVIEW_MIN_DATA_POINTS = 1


@dataclass(frozen=True)
class MaturityVerdict:
    """Outcome of :func:`evaluate`.

    ``mature`` True  → render the real content unchanged.
    ``mature`` False → reframe to "warming up / insufficient data"
    (informational), never warn/red.
    """

    mature: bool
    reason: str  # "" when mature; else a short human-readable why-warming-up
    pod_age_days: float | None  # None when undeterminable
    data_points: int | None  # echoes the producer-supplied count (None if unset)

    @property
    def warming_up(self) -> bool:
        return not self.mature


def evaluate(
    shared_dir,
    *,
    min_age_days: float | None = None,
    min_data_points: int | None = None,
    data_points: int | None = None,
    now: datetime | None = None,
) -> MaturityVerdict:
    """Return a :class:`MaturityVerdict` for a history-dependent producer.

    Parameters
    ----------
    shared_dir
        Pod shared dir (for the pod-age markers).
    min_age_days
        Minimum pod age the producer requires, or ``None`` to not gate on age.
    min_data_points
        Minimum accumulated data-point count the producer requires, or ``None``
        to not gate on data volume.
    data_points
        The producer's *current* accumulated count. Compared against
        ``min_data_points``. If ``min_data_points`` is set but this is ``None``,
        the data dimension fails OPEN (treated as satisfied) — the gate never
        guesses a count.
    now
        Override for the current time (tests).
    """
    age_td = settle_gate.pod_age(shared_dir, now=now)
    pod_age_days = age_td.total_seconds() / 86_400.0 if age_td is not None else None

    reasons: list[str] = []

    # Age dimension. Unknown age (no markers → established/upgraded pod) is
    # treated as old enough: fail OPEN to mature.
    age_ok = True
    if min_age_days is not None and pod_age_days is not None:
        age_ok = pod_age_days >= min_age_days
        if not age_ok:
            days = pod_age_days
            unit = "day" if 0.5 <= days < 1.5 else "days"
            reasons.append(
                f"pod is {days:.0f} {unit} old (< {min_age_days:.0f}-day minimum "
                "for a meaningful weekly comparison)"
            )

    # Data dimension. A declared-but-unsupplied count fails OPEN to mature.
    data_ok = True
    if min_data_points is not None and data_points is not None:
        data_ok = data_points >= min_data_points
        if not data_ok:
            reasons.append(
                f"only {data_points} data point(s) accumulated "
                f"(< {min_data_points} needed to score)"
            )

    mature = age_ok and data_ok
    return MaturityVerdict(
        mature=mature,
        reason="" if mature else "; ".join(reasons),
        pod_age_days=pod_age_days,
        data_points=data_points,
    )
