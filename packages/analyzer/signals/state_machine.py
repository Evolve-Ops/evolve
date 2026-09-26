"""signals.state_machine — Signal lifecycle transitions.

Spec: internal/spec-alerts-signal-store-2026-05-07.md §5.

Legal transitions:

    firing  ──snooze──►  snoozed
    firing  ──resolve──► resolved   (terminal)
    firing  ──dismiss──► dismissed  (terminal)

    snoozed ──timer───►  firing
    snoozed ──resolve──► resolved   (producer auto-resolve while snoozed)

Any attempted illegal transition raises :class:`IllegalTransitionError`
and the Signal is left untouched. Every accepted transition appends a
:class:`StateTransition` to ``signal.state_history``.
"""

from __future__ import annotations

from evolve_util import now_iso_offset as _utc_now_iso
from schema.signal import Signal, State, StateTransition


class IllegalTransitionError(Exception):
    """Raised when a state transition is not permitted."""

    def __init__(self, from_state: str, to_state: str, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        msg = f"Illegal Signal transition: {from_state} → {to_state}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Transition graph
# ─────────────────────────────────────────────────────────────────────────────

_TRANSITIONS: dict[State, frozenset[State]] = {
    "firing": frozenset({"snoozed", "resolved", "dismissed"}),
    "snoozed": frozenset({"firing", "resolved", "dismissed"}),
    # ``resolved`` is reachable back to ``firing`` only through the
    # observe() re-open path: a producer re-observes the same signature
    # within the re-open window (spec §6). Users do not transition
    # resolved → firing directly. Treat ``resolved`` as effectively
    # terminal for UI purposes (``is_terminal`` returns True) but keep
    # the back-edge open so observe() doesn't have to bypass the state
    # machine.
    "resolved": frozenset({"firing"}),
    # ``dismissed`` is the user saying "don't tell me again." Truly
    # terminal — observe() must create a fresh Signal rather than
    # re-opening.
    "dismissed": frozenset(),
}


def allowed_transitions(from_state: State) -> frozenset[State]:
    """Return the set of states ``from_state`` may transition to."""
    return _TRANSITIONS.get(from_state, frozenset())


def is_terminal(state: State) -> bool:
    """True if the state is terminal from a UI/user perspective.

    ``resolved`` has a back-edge to ``firing`` for the observe()
    re-open path, but users cannot transition out of it directly — for
    UI purposes treat it as terminal alongside ``dismissed``.
    """
    return state in {"resolved", "dismissed"}


def transition(
    signal: Signal,
    to_state: State,
    *,
    actor: str,
    reason: str = "",
) -> Signal:
    """Apply a state transition in-place, appending to history.

    Mutates ``signal``. Raises :class:`IllegalTransitionError` if the
    transition is not in the legal graph.

    Side effects beyond ``state`` and ``state_history``:
      - ``firing → resolved``: sets ``resolved_at``
      - ``firing → snoozed``: caller is responsible for setting
        ``snoozed_until`` *before* calling transition (the helper does
        not invent a timer)
      - ``snoozed → firing`` (timer): clears ``snoozed_until``
    """
    if to_state not in allowed_transitions(signal.state):
        raise IllegalTransitionError(signal.state, to_state)

    from_state = signal.state
    signal.state = to_state
    signal.state_history.append(
        StateTransition(
            from_state=from_state,
            to_state=to_state,
            at=_utc_now_iso(),
            actor=actor,
            reason=reason,
        )
    )

    if to_state == "resolved":
        signal.resolved_at = signal.resolved_at or _utc_now_iso()
    if from_state == "snoozed" and to_state == "firing":
        signal.snoozed_until = None

    return signal
