"""signals.auto_remediating — the "known scheduled self-heal" condition class.

Spec: docs/spec-delta-transient-delivery-grace-2026-06-26.md (L2) under the
umbrella docs/spec-transient-signal-suppression-2026-06-23.md.

Some conditions page the operator while a *scheduled job is already guaranteed
to fix them* with zero operator action. The canonical case is
``pod_perms_drift`` (the Alerts/Subscriptions catalog calls this "config
drift"): ``ensure_pod_perms`` only runs at deploy time, so this monitor exists
to catch drift *between* deploys — and the very next deploy (~15-min cadence)
silently re-applies the contract. Paging about a thing a scheduled job fixes,
with nothing for the operator to do, is noise.

This registry names those types and their expected self-heal window. The rule
applied at the delivery boundary (see
``evolve_admin.alerts.signal_notifier``) is **"page on the FAILURE of the
auto-fix, not on the condition"**: suppress the operator notification until the
condition has been *continuously firing longer than its self-heal window and
is still firing*. A condition that self-clears inside the window is recorded on
the Alerts page (we never touch the store) but never pushed to chat/digest.

Relationship to the producer-side gates
=======================================

This is purely a *delivery* policy — it lives next to
:mod:`signals.flap_gate` / :mod:`signals.settle_gate` for discoverability but
is consulted at the notifier, NOT the producer. It therefore **composes** with
flap_gate rather than replacing it:

  * ``flap_gate`` (producer-side, N≥2 dwell) proves the condition is *real*
    before it ever reaches the Signal store.
  * this registry (notify-side) proves the condition is *durable past its own
    auto-fix* before the operator is told.

Critical composition invariant
==============================

This NEVER applies to ``alert``/critical severity. A critical condition is
paged immediately regardless of registration; the suppression-until-self-heal
window is only ever consulted for non-alert (``warn``/``info``) signals. The
caller is responsible for the severity guard, but :func:`self_heal_window` only
returns a window for registered *types* — see :func:`should_suppress` for the
composed check that bakes the alert guard in.

ACT-SIDE follow-up (out of scope here)
======================================

The root fix for ``pod_perms_drift`` is to *proactively* run
``ensure_pod_perms`` the moment drift is detected between deploys — actually
remediating in minutes instead of waiting for the next deploy — rather than
merely suppressing the page. That is owned by ``edr``/``deploy`` and is
explicitly out of scope for this notify-side chip. See the spec's "act-side
root fix" note (§L2).
"""

from __future__ import annotations

# Default self-heal window for a registered type that doesn't specify its own.
# Must comfortably exceed one deploy cycle (~15-min cadence) so a condition the
# next deploy will fix is never paged. 30 min = two deploy cycles of headroom.
DEFAULT_SELF_HEAL_WINDOW_SECONDS = 1800


# Signal ``type`` → expected self-heal window in seconds. A condition of this
# type is suppressed at the delivery boundary until it has been continuously
# firing longer than its window (and is still firing) — i.e. until the
# scheduled self-heal has demonstrably FAILED to clear it.
#
# Keyed on the Signal ``type`` string (the value the producer passes to
# ``signals.store.observe(type=...)``), NOT the catalog/subscription event:
# ``pod_perms_drift_monitor.py`` emits ``type="pod_perms_drift"`` (the
# Subscriptions UI surfaces it under the ``security.config_drift`` event).
#
# First and only member: pod_perms_drift. ``ensure_pod_perms`` re-applies the
# perm contract on every deploy (~15-min cadence), so drift caught between
# deploys self-heals within one cycle. We give it the 30-min default (two
# cycles) of headroom before paging.
AUTO_REMEDIATING_WINDOW_BY_TYPE: dict[str, int] = {
    "pod_perms_drift": DEFAULT_SELF_HEAL_WINDOW_SECONDS,
}


def self_heal_window(type_: str | None) -> int | None:
    """Expected self-heal window (seconds) for a Signal ``type``, or None.

    None means the type is NOT registered as auto-remediating — the caller
    should page it on its normal schedule. A registered type returns its
    per-type window (falling back to :data:`DEFAULT_SELF_HEAL_WINDOW_SECONDS`
    if it was registered with a non-positive/garbage value)."""
    if not type_:
        return None
    if type_ not in AUTO_REMEDIATING_WINDOW_BY_TYPE:
        return None
    window = AUTO_REMEDIATING_WINDOW_BY_TYPE[type_]
    try:
        window = int(window)
    except (TypeError, ValueError):
        return DEFAULT_SELF_HEAL_WINDOW_SECONDS
    return window if window > 0 else DEFAULT_SELF_HEAL_WINDOW_SECONDS


def should_suppress(
    *,
    type_: str | None,
    severity: str | None,
    firing_age_seconds: float | None,
) -> bool:
    """Whether to SUPPRESS the operator notification for a firing signal.

    Returns ``True`` only when ALL of:

      * ``severity`` is non-alert (``alert``/critical is NEVER suppressed —
        this is the keystone composition invariant);
      * ``type_`` is a registered auto-remediating type;
      * the signal has been firing for *less* than its self-heal window
        (``firing_age_seconds < window``) — i.e. its scheduled self-heal has
        not yet had time to fail.

    Once the condition outlives its window and is still firing, this returns
    ``False`` and the notifier pages normally ("the auto-fix failed"). A
    ``None`` / unparseable age fails OPEN (returns ``False`` → page) so a
    missing timestamp can never silently swallow a persistent condition.
    """
    # alert/critical is never suppressed or delayed — guard first and hardest.
    if severity == "alert":
        return False
    window = self_heal_window(type_)
    if window is None:
        return False  # not auto-remediating → page on the normal schedule
    if firing_age_seconds is None:
        return False  # unknown age → fail open (page)
    return firing_age_seconds < window


__all__ = (
    "AUTO_REMEDIATING_WINDOW_BY_TYPE",
    "DEFAULT_SELF_HEAL_WINDOW_SECONDS",
    "self_heal_window",
    "should_suppress",
)
