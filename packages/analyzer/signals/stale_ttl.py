"""signals.stale_ttl — TTL backstop for firing Signals nobody resolves.

Spec lineage: internal/spec-alerts-signal-store-2026-05-07.md §6 says
event-style monitors "rely on TTL or manual resolution" — but the TTL
was never built, so any producer that forgets its ``sweep_resolve``
leaks firing Signals unboundedly (59 of the mini's 112 firing signals
dated from June/July as of the 2026-08-30 alerts-noise review,
internal/review-alerts-findings-noise-2026-08-30.md Track 1.2).

This module is that backstop, run from the daily retention pass
(:func:`signals.retention.prune_retention`):

* Any **firing** Signal whose ``last_observed_at`` is older than
  ``ttl_days`` (default 14) transitions to ``resolved`` through the
  normal state machine, with an explicit reason naming the backstop.
  A healthy producer re-observes a still-true condition on every run,
  which bumps ``last_observed_at`` — so only conditions no producer is
  maintaining ever age out.
  The resolution carries ``resolution_kind="ttl_backstop"``
  (:data:`schema.signal.TTL_BACKSTOP_RESOLUTION`) so downstream
  consumers can tell this weaker claim apart from a producer's
  ``sweep_resolve``, which did observe the condition clear.
* **Snoozed** Signals are never touched — a snooze is an operator
  statement with its own timer (``wake_due_snoozes``), and the TTL has
  no business overriding it.
* When the backstop archives anything, it emits ONE ``warn`` meta-Signal
  per leaking producer (producer ``signals_retention``, type
  ``producer_signal_leak``, value-free per-producer signature) so a
  missing sweep becomes self-reporting instead of silent. Runs that find
  a previously-leaking producer clean sweep-resolve its meta-Signal.

Opting out (operator-acked-only signal types)
---------------------------------------------
A few signal types are *deliberately* long-lived reminders that only the
operator resolves — auto-archiving them would silently drop a real TODO.
A producer opts such a type out by adding it to :data:`TTL_EXEMPT_TYPES`
with a comment saying why it is operator-acked-only. Exemption is
per-``type`` (not per-producer) so a producer with both event-style and
reminder-style signals only exempts the reminder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schema.signal import TTL_BACKSTOP_RESOLUTION, make_signature
from signals import store as signals_store
from signals.state_machine import IllegalTransitionError

DEFAULT_STALE_FIRING_TTL_DAYS = 14

# The meta-signal's own identity.
LEAK_PRODUCER = "signals_retention"
LEAK_TYPE = "producer_signal_leak"

# Signal types the TTL backstop must never auto-resolve. Each entry is a
# deliberate operator-acked-only reminder; add a comment saying why.
TTL_EXEMPT_TYPES: frozenset[str] = frozenset({
    # oc_upgrade_runtime_notes_reminder: post-upgrade nudge to walk
    # docs/system/RUNTIME_NOTES.md. Operator-acked by design; superseded
    # by the next version's reminder (ocadmin._emit_runtime_notes_review_signal),
    # never by the clock.
    "runtime_notes_review_due",
    # The backstop's own meta-signal: its lifecycle is fully managed by
    # this module's sweep_resolve (clean run ⇒ resolved), so the TTL pass
    # skipping it keeps the two mechanisms from chasing each other.
    LEAK_TYPE,
})


@dataclass
class StaleTtlResult:
    """Outcome of one :func:`sweep_stale_firing` run."""

    stale_resolved: int = 0
    exempt_skipped: int = 0
    leak_signals_emitted: int = 0
    leak_signals_resolved: int = 0
    # producer → count of that producer's signals the TTL archived.
    resolved_by_producer: dict[str, int] = field(default_factory=dict)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def sweep_stale_firing(
    shared_dir: Path,
    *,
    ttl_days: int = DEFAULT_STALE_FIRING_TTL_DAYS,
    now: datetime | None = None,
) -> StaleTtlResult:
    """Resolve firing Signals not re-observed within ``ttl_days``.

    ``ttl_days <= 0`` disables the backstop entirely (no TTL sweep, no
    meta-signal bookkeeping). ``now`` is injectable for tests.

    All mutations go through the store APIs (``apply_transition`` /
    ``observe`` / ``sweep_resolve``) — never raw file edits — so state
    history, the state-change log, and subdir routing stay correct.
    """
    result = StaleTtlResult()
    if ttl_days <= 0:
        return result
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=ttl_days)

    reason = (
        f"stale: not re-observed for {ttl_days}d — TTL backstop "
        "(producer likely missing sweep_resolve)"
    )
    # producer → set of aged-out signal types (for the meta-signal body).
    aged_types: dict[str, set[str]] = {}
    for sig in list(
        signals_store.iter_signals(shared_dir, subdirs=("firing",))
    ):
        if sig.type in TTL_EXEMPT_TYPES:
            result.exempt_skipped += 1
            continue
        last_observed = _parse_iso(sig.last_observed_at)
        if last_observed is None:
            # Unparseable timestamp — keep, don't risk archiving a live
            # condition on a read failure.
            continue
        if last_observed >= cutoff:
            continue
        try:
            signals_store.apply_transition(
                sig,
                "resolved",
                shared_dir,
                actor=LEAK_PRODUCER,
                reason=reason,
                # Structured marker: this resolution means "nobody
                # re-observed it", NOT "the condition cleared". Readers
                # that act on the stronger claim (arbiter.auto_resolve)
                # gate on this rather than on the reason prose above.
                resolution_kind=TTL_BACKSTOP_RESOLUTION,
            )
        except IllegalTransitionError:
            # Lost a race to a concurrent transition; the fresh state is
            # whatever won — leave it.
            continue
        result.stale_resolved += 1
        result.resolved_by_producer[sig.producer] = (
            result.resolved_by_producer.get(sig.producer, 0) + 1
        )
        aged_types.setdefault(sig.producer, set()).add(sig.type)

    result.leak_signals_emitted, result.leak_signals_resolved = (
        _report_leaking_producers(
            shared_dir,
            resolved_by_producer=result.resolved_by_producer,
            aged_types=aged_types,
            ttl_days=ttl_days,
        )
    )
    return result


def _report_leaking_producers(
    shared_dir: Path,
    *,
    resolved_by_producer: dict[str, int],
    aged_types: dict[str, set[str]],
    ttl_days: int,
) -> tuple[int, int]:
    """Emit/refresh one leak meta-Signal per leaking producer; resolve
    the meta-Signals of producers that came back clean this run.

    Returns ``(emitted, resolved)`` counts.
    """
    kept_signatures: set[str] = set()
    emitted = 0
    for producer in sorted(resolved_by_producer):
        count = resolved_by_producer[producer]
        types = sorted(aged_types.get(producer, set()))
        # Value-free signature: per-producer only, no counts/dates — the
        # same leak re-observed on later runs bumps one Signal instead of
        # minting siblings.
        signature = make_signature(LEAK_PRODUCER, LEAK_TYPE, producer)
        kept_signatures.add(signature)
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer=LEAK_PRODUCER,
            type=LEAK_TYPE,
            # severity/flavor inherit the producer defaults declared in
            # producer_severity.py (warn / maintenance).
            scope="pod",
            category="hygiene",
            title=f"Signal producer '{producer}' is leaking firing signals",
            body=(
                f"The stale-firing TTL backstop archived {count} signal(s) "
                f"from producer `{producer}` (types: {', '.join(types)}) "
                f"because they were not re-observed for {ttl_days} days.\n\n"
                "A healthy producer either re-observes a still-true "
                "condition every run or calls "
                "`signals.store.sweep_resolve(...)` for conditions that "
                "cleared. This producer is doing neither for these types — "
                "its signals only leave the queue when the TTL backstop "
                "ages them out. Fix the producer's resolve path; if a type "
                "is deliberately operator-acked-only, exempt it in "
                "`signals.stale_ttl.TTL_EXEMPT_TYPES` instead."
            ),
            details={
                "leaking_producer": producer,
                "aged_out_count": count,
                "aged_out_types": types,
                "ttl_days": ttl_days,
            },
        )
        emitted += 1

    resolved = len(
        signals_store.sweep_resolve(
            shared_dir,
            producer=LEAK_PRODUCER,
            kept_signatures=kept_signatures,
            types={LEAK_TYPE},
            reason=(
                "auto-resolve: no signals from this producer aged out on "
                "the latest retention run"
            ),
        )
    )
    return emitted, resolved
