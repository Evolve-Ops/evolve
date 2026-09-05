"""Per-severity delivery grace — the "time threshold for issues" substrate.

Spec: internal/spec-delta-transient-delivery-grace-2026-06-26.md (Chip A, L3).
Umbrella: internal/spec-transient-signal-suppression-2026-06-23.md.

The operator asked for *"some time threshold for issues so we don't waste
operators' time on transient issues and blips."* This module is that
threshold, generalized to a **per-severity grace window** that two delivery
surfaces consult:

  - **signal_notifier** (real-time): a firing Signal younger than its
    severity grace is held back; if it resolves before the grace elapses
    the operator is never paged (the old hardcoded ~240s debounce, now
    severity-scaled).
  - **digest_dispatcher** (L1 fire+clear cancellation): a fire and its
    matching resolve that both land in one digest window, for a Signal whose
    *lifetime* was shorter than its severity grace, are elided as a pair.

The two surfaces share ONE definition of the threshold so "a warn that
clears inside grace" means the same thing whether it's caught live or in the
digest.

Invariant — ``alert``/critical is never delayed
-----------------------------------------------
``alert`` grace is **clamped to zero** (``_ALERT_GRACE_CEILING_SECONDS``)
regardless of operator config. A critical condition must page near-immediately;
a fat-fingered ``alerts.grace_seconds_by_severity.alert: 3600`` can never
silently sit on a real outage. The clamp lives here so both surfaces inherit
it for free.

Distinct from the flap window
------------------------------
This is NOT the recurring-flap window
(``alerts.signal_notifier.flap_window_seconds``). Grace gates a *first* fire
by its own age; the flap window gates a *re-fire* by the time since the last
resolve. They compose — a brand-new signal is graced; a signal that already
paged, cleared, and re-fires is flap-suppressed — and stay separate knobs.
"""

from __future__ import annotations

from pathlib import Path

from . import _config_lookup

# Severity → default grace in seconds. The operator's literal "time
# threshold" per the spec's L3 table:
#   * alert — ~0 (near-immediate); CLAMPED, never delayed.
#   * warn  — ~one digest window (a within-window transient warn never
#             reaches chat).
#   * info  — same shape; info is already hidden-by-default on the Alerts
#             page, so a generous grace costs the operator nothing.
# Operator-tunable as a whole via ``alerts.grace_seconds_by_severity``;
# missing per-severity keys fall back to these.
_DEFAULT_GRACE_SECONDS_BY_SEVERITY: dict[str, int] = {
    "alert": 0,
    "warn": 900,
    "info": 900,
}

# Hard ceiling on the ``alert`` grace. The clamp is the keystone of the
# "alert is never delayed" invariant — no config value can push a critical
# past this. Zero ⇒ a firing alert pages on the very next tick, exactly as
# before this substrate existed.
_ALERT_GRACE_CEILING_SECONDS = 0

# Fallback when a Signal carries an unrecognized severity. Treat it as a
# warn-shaped condition (the central DEFAULT_SEVERITY) rather than zero —
# an unknown-severity transient is still a candidate for grace, but we never
# clamp it the way we clamp a *known* alert.
_UNKNOWN_SEVERITY_GRACE_KEY = "warn"


def read_grace_seconds_by_severity(shared_dir: Path) -> dict[str, int]:
    """Resolve the per-severity grace map from config, defaults-merged.

    Reads ``alerts.grace_seconds_by_severity`` (operator override) and
    layers it over ``_DEFAULT_GRACE_SECONDS_BY_SEVERITY`` so a partial
    override (e.g. only ``warn``) keeps sane defaults for the rest. Any
    non-int / negative value for a key falls back to that key's default.

    The ``alert`` entry is ALWAYS clamped to ``_ALERT_GRACE_CEILING_SECONDS``
    after the merge — operator config cannot delay a critical (the invariant).
    """
    merged = dict(_DEFAULT_GRACE_SECONDS_BY_SEVERITY)
    raw = _config_lookup.lookup(shared_dir, "alerts.grace_seconds_by_severity", {})
    if isinstance(raw, dict):
        for sev, default in _DEFAULT_GRACE_SECONDS_BY_SEVERITY.items():
            if sev not in raw:
                continue
            try:
                val = int(raw[sev])
            except (TypeError, ValueError):
                continue
            if val < 0:
                continue
            merged[sev] = val
    # The invariant clamp — applied last so no override survives it.
    merged["alert"] = _ALERT_GRACE_CEILING_SECONDS
    return merged


def grace_for_severity(shared_dir: Path, severity: str) -> int:
    """Grace window (seconds) for a single severity, clamp-respecting.

    Convenience over :func:`read_grace_seconds_by_severity` for the common
    one-severity lookup. An unrecognized severity is treated as the
    ``warn``-shaped default (never the alert clamp — we only zero a *known*
    alert)."""
    table = read_grace_seconds_by_severity(shared_dir)
    if severity in table:
        return table[severity]
    return table.get(_UNKNOWN_SEVERITY_GRACE_KEY, 0)


def within_grace(shared_dir: Path, severity: str, age_or_lifetime_seconds: float) -> bool:
    """True when ``age_or_lifetime_seconds`` is strictly inside the severity grace.

    Used by both surfaces: signal_notifier passes a firing Signal's *age*
    (a younger-than-grace fire is held), and digest_dispatcher passes a
    resolved Signal's *lifetime* (a shorter-than-grace fire+clear pair is a
    cancellation candidate). ``alert`` always returns ``False`` (grace 0) —
    the clamp guarantees a critical is never "within grace".
    """
    grace = grace_for_severity(shared_dir, severity)
    if grace <= 0:
        return False
    return age_or_lifetime_seconds < grace


__all__ = (
    "read_grace_seconds_by_severity",
    "grace_for_severity",
    "within_grace",
)
