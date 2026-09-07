"""tests/test_ttl_autoresolve_cascade.py — signal TTL → proposal archive.

PR #3865 gave firing Signals a 14-day TTL backstop
(``signals.stale_ttl.sweep_stale_firing``). It did not introduce a
defect, but it made one *reachable*: ``arbiter.auto_resolve`` decides a
proposal's motivating condition has "cleared" by reading only the
signal's ``state``, so a TTL resolution — which means nothing more than
"no producer re-observed this for 14 days" — read identically to a
``sweep_resolve`` that actually watched the condition clear. The daily
jobs are 15 minutes apart by design (retention 03:30, auto-resolve
03:45), so the cascade runs nightly on every pod, and
``resolved_externally`` is terminal (``state_machine.py``:
``frozenset()``) with no un-archive.

These tests pin the two guards added 2026-08-31 and, just as
importantly, pin the behavior that must NOT change:

* an operator-engaged proposal is held when its "cleared" verdict rests
  on a TTL-resolved signal;
* an *unengaged* proposal still archives — Rule 1 is narrowed, not
  disabled (the regression a too-broad fix would cause);
* a genuine ``sweep_resolve`` clearance still archives, engaged or not;
* snoozed proposals are never archived by the sweep at all;
* the missing-signal-file branch (retention pruned it past 90 days) is
  untouched — its justification is retention age, not the TTL;
* ``ttl_days=0`` (backstop disabled) leaves pre-#3865 behavior intact.

Everything runs the REAL cascade — ``sweep_stale_firing`` then
``sweep_auto_resolve`` against one ``shared_dir`` — and asserts the
proposal's status and subdir **on disk**, never a return-value counter
alone. Signals are created and mutated only through the store APIs, so
state history, subdir routing and the state-change log stay honest.

Clock-coupling: signals are backdated by rewriting ``last_observed_at``
through ``signals_store.write_signal`` (a store API, not a hand edit);
the margins (14d TTL vs 30d backdates) dwarf any test-run wall-clock
drift.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.auto_resolve import sweep_auto_resolve  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import proposal_path, write_proposal  # noqa: E402
from schema.signal import TTL_BACKSTOP_RESOLUTION, Signal  # noqa: E402
from signals import stale_ttl, store as signals_store  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — store APIs only
# ─────────────────────────────────────────────────────────────────────────────


def _observe(
    shared_dir: Path,
    *,
    producer: str = "quiet_producer",
    type_: str = "quiet_type",
    scope_key: str = "all",
) -> Signal:
    """Create a firing Signal the way a real producer would."""
    return signals_store.observe(
        shared_dir,
        signature=f"{producer}:{type_}:{scope_key}",
        producer=producer,
        type=type_,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=f"{producer} {type_}",
    )


def _backdate(shared_dir: Path, sig: Signal, *, days: int) -> None:
    """Push last_observed_at into the past via the store API."""
    sig.last_observed_at = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    signals_store.write_signal(sig, shared_dir, subdir="firing")


def _age_out(shared_dir: Path, sig: Signal) -> Signal:
    """Run the REAL TTL backstop over a signal quiet for 30 days."""
    _backdate(shared_dir, sig, days=30)
    result = stale_ttl.sweep_stale_firing(shared_dir)
    assert result.stale_resolved >= 1
    found = signals_store.find_signal(shared_dir, sig.id)
    assert found is not None
    fresh, _path, _subdir = found
    assert fresh.state == "resolved"
    return fresh


def _sweep_resolve(shared_dir: Path, sig: Signal) -> Signal:
    """Resolve a signal the honest way: the producer observed it clear."""
    signals_store.sweep_resolve(
        shared_dir,
        producer=sig.producer,
        kept_signatures=set(),
        reason="auto-resolve: condition cleared",
    )
    found = signals_store.find_signal(shared_dir, sig.id)
    assert found is not None
    fresh, _path, _subdir = found
    assert fresh.state == "resolved"
    return fresh


def _proposal(
    shared_dir: Path,
    signal_ids: list[str],
    *,
    engaged: bool = False,
    snoozed: bool = False,
    bot_id: str = "bot_c",
):
    """A proposal motivated by ``signal_ids``.

    ``engaged`` adds a snooze+wake pair — the same shape the existing
    tier3 tests use for "the operator touched this". ``snoozed`` leaves
    it parked in the snoozed state.
    """
    p = make_investigation_proposal(bot_id=bot_id, problem="motivated")
    transition(p, "pending", actor="test")
    p.motivating_signals = list(signal_ids)
    if engaged:
        transition(p, "snoozed", actor="user")
        transition(p, "pending", actor="snooze_wake")
    if snoozed:
        transition(p, "snoozed", actor="user")
    write_proposal(p, shared_dir)
    return p


def _status_on_disk(shared_dir: Path, proposal_id: str, subdir: str) -> str:
    """Read the status field straight out of the stored JSON."""
    path = proposal_path(shared_dir, proposal_id, subdir=subdir)
    return json.loads(path.read_text())["status"]


# ─────────────────────────────────────────────────────────────────────────────
# The marker itself — prove a live declarer writes what the gate reads
# ─────────────────────────────────────────────────────────────────────────────


def test_real_ttl_sweep_writes_the_marker_the_gate_reads(tmp_path):
    """A real sweep_stale_firing run persists resolution_kind on disk.

    Guards against the "gate keyed on a vocabulary no writer emits"
    class: the constant auto_resolve reads must be the one stale_ttl
    actually writes, verified through a full round-trip to JSON.
    """
    sig = _observe(tmp_path)
    _backdate(tmp_path, sig, days=30)

    stale_ttl.sweep_stale_firing(tmp_path)

    raw = json.loads(
        signals_store.signal_path(tmp_path, sig.id, subdir="archived").read_text()
    )
    assert raw["state"] == "resolved"
    assert raw["resolution_kind"] == TTL_BACKSTOP_RESOLUTION


def test_sweep_resolve_leaves_the_marker_unset(tmp_path):
    """A genuine producer clearance is NOT marked ttl_backstop — the two
    resolution paths must stay distinguishable on disk."""
    sig = _observe(tmp_path)

    fresh = _sweep_resolve(tmp_path, sig)

    assert fresh.resolution_kind is None
    raw = json.loads(
        signals_store.signal_path(tmp_path, sig.id, subdir="archived").read_text()
    )
    assert raw["resolution_kind"] is None


def test_reopen_clears_the_ttl_marker(tmp_path):
    """A TTL-resolved signal that its producer starts reporting again is
    firing, and must not carry a stale marker into its next resolution."""
    sig = _observe(tmp_path)
    _age_out(tmp_path, sig)

    reopened = _observe(tmp_path)  # same signature → re-open within window

    assert reopened.id == sig.id
    assert reopened.state == "firing"
    assert reopened.resolution_kind is None


def test_legacy_signal_without_the_field_loads_as_unmarked(tmp_path):
    """Back-compat: signals resolved before the field existed have no
    ``resolution_kind`` key and must read as an ordinary resolution —
    the pre-existing archive behavior, not the new hold."""
    sig = _observe(tmp_path)
    _sweep_resolve(tmp_path, sig)
    path = signals_store.signal_path(tmp_path, sig.id, subdir="archived")
    raw = json.loads(path.read_text())
    del raw["resolution_kind"]
    path.write_text(json.dumps(raw))

    found = signals_store.find_signal(tmp_path, sig.id)

    assert found is not None
    assert found[0].resolution_kind is None


# ─────────────────────────────────────────────────────────────────────────────
# The cascade — TTL resolution → auto-resolve archive
# ─────────────────────────────────────────────────────────────────────────────


def test_ttl_resolved_signal_holds_engaged_proposal(tmp_path):
    """THE FINDING. Full cascade, both sweeps, one shared_dir.

    An operator-engaged proposal whose only motivating signal aged out
    on the TTL must survive: "we stopped hearing about it" does not
    support the claim ``resolved_externally`` makes.
    """
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], engaged=True)
    _age_out(tmp_path, sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_ttl_engaged == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()
    assert not proposal_path(tmp_path, p.id, subdir="archived").exists()
    assert _status_on_disk(tmp_path, p.id, "pending") == "pending"


def test_ttl_resolved_signal_still_archives_unengaged_proposal(tmp_path):
    """Rule 1 is NARROWED, not disabled.

    Same cascade, no operator engagement — the proposal still archives.
    A fix that held everything would re-create the net-additive queue
    #3865 was built to drain, and no other test would catch it.
    """
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], engaged=False)
    _age_out(tmp_path, sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert result.proposals_skipped_ttl_engaged == 0
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()
    assert _status_on_disk(tmp_path, p.id, "archived") == "resolved_externally"


def test_genuine_clearance_still_archives_engaged_proposal(tmp_path):
    """THE REGRESSION GUARD. The condition really did clear (the
    producer's own sweep_resolve said so), so engagement does not hold
    it — only the TTL's weaker claim triggers the new gate."""
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], engaged=True)
    _sweep_resolve(tmp_path, sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert result.proposals_skipped_ttl_engaged == 0
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()
    assert _status_on_disk(tmp_path, p.id, "archived") == "resolved_externally"


def test_mixed_signals_one_ttl_one_swept_holds_engaged_proposal(tmp_path):
    """``_all_motivating_signals_inactive`` is an all(); the TTL taint is
    an any(). With a mixed set the "everything cleared" verdict rests
    partly on a claim nobody verified, so the whole verdict inherits the
    weaker footing."""
    ttl_sig = _observe(tmp_path, producer="quiet_producer", scope_key="a")
    swept_sig = _observe(tmp_path, producer="live_producer", scope_key="b")
    p = _proposal(tmp_path, [ttl_sig.id, swept_sig.id], engaged=True)
    _sweep_resolve(tmp_path, swept_sig)
    _age_out(tmp_path, ttl_sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_ttl_engaged == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_mixed_signals_hold_requires_all_inactive_first(tmp_path):
    """The pre-existing precondition is unchanged: one still-firing
    signal keeps the proposal pending via the ordinary active-signal
    path, not the new gate."""
    ttl_sig = _observe(tmp_path, producer="quiet_producer", scope_key="a")
    live_sig = _observe(tmp_path, producer="live_producer", scope_key="b")
    p = _proposal(tmp_path, [ttl_sig.id, live_sig.id], engaged=True)
    _age_out(tmp_path, ttl_sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_skipped_signals_active == 1
    assert result.proposals_skipped_ttl_engaged == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Snooze exemption
# ─────────────────────────────────────────────────────────────────────────────


def test_snoozed_proposal_survives_ttl_resolved_signals(tmp_path):
    """The sharpest case: a snoozed proposal archived terminally could
    never be recovered by snooze_wake, because it no longer lives in
    snoozed/. stale_ttl already refuses to override a snoozed Signal;
    the proposal side now mirrors that."""
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], snoozed=True)
    _age_out(tmp_path, sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_snoozed == 1
    assert proposal_path(tmp_path, p.id, subdir="snoozed").exists()
    assert _status_on_disk(tmp_path, p.id, "snoozed") == "snoozed"


def test_snoozed_proposal_survives_genuine_clearance(tmp_path):
    """The exemption is unconditional — it is about the operator's
    statement, not about which mechanism resolved the signal. The
    decision is deferred to snooze_wake, not cancelled."""
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], snoozed=True)
    _sweep_resolve(tmp_path, sig)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_snoozed == 1
    assert proposal_path(tmp_path, p.id, subdir="snoozed").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Branches that must NOT change
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_signal_file_still_archives_engaged_proposal(tmp_path):
    """Retention pruned the signal past 90 days. That branch's
    justification is retention age, not the TTL backstop, so it is not
    marked ttl_resolved and the engagement gate leaves it alone."""
    p = _proposal(tmp_path, ["signal-that-no-longer-exists"], engaged=True)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert result.proposals_skipped_ttl_engaged == 0
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()
    assert _status_on_disk(tmp_path, p.id, "archived") == "resolved_externally"


def test_ttl_days_zero_leaves_rule_1_untouched(tmp_path):
    """Backstop disabled → the signal stays firing → the proposal stays
    pending through the ordinary active-signal path. Pre-#3865
    behavior, unchanged."""
    sig = _observe(tmp_path)
    p = _proposal(tmp_path, [sig.id], engaged=True)
    _backdate(tmp_path, sig, days=30)

    ttl_result = stale_ttl.sweep_stale_firing(tmp_path, ttl_days=0)
    result = sweep_auto_resolve(tmp_path)

    assert ttl_result.stale_resolved == 0
    assert result.proposals_resolved == 0
    assert result.proposals_skipped_signals_active == 1
    assert result.proposals_skipped_ttl_engaged == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_empty_motivating_signals_still_skipped(tmp_path):
    """Unchanged: no link, no reasoning about the condition."""
    p = _proposal(tmp_path, [], engaged=True)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_skipped_no_signals == 1
    assert result.proposals_resolved == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_unparseable_created_at_never_archives_on_a_refresh_clock(tmp_path):
    """Fail-safe preserved. Rule 2 refuses to archive a proposal whose
    ``created_at`` will not parse — there is no trustworthy staleness
    clock. A dedup-refresh entry carries a valid timestamp, but it may
    only ADVANCE an existing reference, never supply the missing one."""
    from schema.proposal import StatusTransition

    p = make_investigation_proposal(
        bot_id="bot_corrupt", generator_id="engagement_amplifier"
    )
    transition(p, "pending", actor="test")
    p.created_at = "not-a-timestamp"
    p.history.append(
        StatusTransition(
            from_status="pending",
            to_status="pending",
            at=(
                datetime.now(timezone.utc) - timedelta(days=90)
            ).isoformat(timespec="seconds"),
            actor="arbiter",
            reason="dedup-refresh from prop-xyz",
        )
    )
    write_proposal(p, tmp_path, coalesce=False)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()
