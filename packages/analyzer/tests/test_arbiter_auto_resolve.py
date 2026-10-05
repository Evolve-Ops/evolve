"""tests/test_arbiter_auto_resolve.py — auto-archive of stale proposals.

Covers the 2026-05-12 design gap that left ~half of the pending
proposals on the mini stale after their motivating signals had
cleared (efficiency_hawk heartbeat_no_model_override proposals
where the bot config had since been fixed).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.auto_resolve import sweep_auto_resolve  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import (  # noqa: E402
    proposal_path,
    write_proposal,
)
from schema.signal import Signal, new_signal_id  # noqa: E402
from signals import store as signals_store  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_signal(*, state: str, signature: str = "test:sig:1") -> Signal:
    """Build a Signal with the given state; id is fresh each call."""
    sig = Signal(
        id=new_signal_id(),
        signature=signature,
        producer="test",
        type="test_signal",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="test_bot",
        title="test",
        body="test",
    )
    sig.state = state  # type: ignore[assignment]
    return sig


def _write_signal(shared_dir: Path, sig: Signal) -> Signal:
    """Write the Signal to the matching subdir based on its state."""
    subdir = {
        "firing": "firing",
        "snoozed": "snoozed",
        "resolved": "archived",
        "dismissed": "archived",
    }[sig.state]
    signals_store.write_signal(sig, shared_dir, subdir=subdir)  # type: ignore[arg-type]
    return sig


def _make_pending_proposal_with_signals(
    shared_dir: Path, signal_ids: list[str]
):
    p = make_investigation_proposal(bot_id="bot_x", problem="motivated")
    transition(p, "pending", actor="test")
    p.motivating_signals = list(signal_ids)
    write_proposal(p, shared_dir)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Core sweep behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_resolves_when_all_motivating_signals_resolved(tmp_path):
    """All signals are resolved → proposal moves to archived as
    resolved_externally."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:1"))
    s2 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:2"))
    p = _make_pending_proposal_with_signals(tmp_path, [s1.id, s2.id])

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_scanned == 1
    assert result.proposals_resolved == 1
    assert p.id in result.resolved_ids
    assert not proposal_path(tmp_path, p.id, subdir="pending").exists()
    archived = proposal_path(tmp_path, p.id, subdir="archived")
    assert archived.exists()
    import json
    payload = json.loads(archived.read_text())
    assert payload["status"] == "resolved_externally"
    # Status history records the transition
    last = payload["history"][-1]
    assert last["to_status"] == "resolved_externally"
    assert last["actor"] == "auto_resolver"


def test_resolves_when_mix_of_resolved_and_dismissed(tmp_path):
    """Dismissed signals count as 'cleared' for this purpose."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:1"))
    s2 = _write_signal(tmp_path, _make_signal(state="dismissed", signature="s:2"))
    p = _make_pending_proposal_with_signals(tmp_path, [s1.id, s2.id])

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_skips_when_any_motivating_signal_firing(tmp_path):
    """Even one firing signal pins the proposal — sweep must not archive."""
    s_active = _write_signal(tmp_path, _make_signal(state="firing", signature="s:a"))
    s_done = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:b"))
    p = _make_pending_proposal_with_signals(tmp_path, [s_active.id, s_done.id])

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_signals_active == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_skips_when_any_motivating_signal_snoozed(tmp_path):
    """Snoozed is still active — the user said 'remind me later,' not
    'this is fixed.'"""
    s_snoozed = _write_signal(tmp_path, _make_signal(state="snoozed", signature="s:z"))
    p = _make_pending_proposal_with_signals(tmp_path, [s_snoozed.id])

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_signals_active == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_treats_missing_signal_as_inactive(tmp_path):
    """A proposal that references a signal id no longer on disk
    (retention swept it) should be considered cleared on that axis —
    otherwise old proposals get pinned forever by 90-day-old refs."""
    p = _make_pending_proposal_with_signals(tmp_path, ["nonexistent-id-1"])

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_skips_proposals_with_empty_motivating_signals(tmp_path):
    """We don't have a link to verify the underlying condition. Leave
    these for the human to triage."""
    p = make_investigation_proposal(bot_id="bot_y")
    transition(p, "pending", actor="test")
    # motivating_signals defaults to [] — explicitly assert
    assert p.motivating_signals == []
    write_proposal(p, tmp_path)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_no_signals == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_skips_applied_proposals(tmp_path):
    """The sweep iterates pending+snoozed only. Applied proposals are
    awaiting verify — their signal-clearance has separate semantics
    (the verify daemon checks claim metrics)."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:c"))
    p = make_investigation_proposal(bot_id="bot_z")
    transition(p, "pending", actor="test")
    transition(p, "approved_auto", actor="test")
    transition(p, "applied", actor="applier")
    p.motivating_signals = [s1.id]
    write_proposal(p, tmp_path)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_scanned == 0  # iter skipped applied/
    assert result.proposals_resolved == 0
    assert proposal_path(tmp_path, p.id, subdir="applied").exists()


def test_snoozed_proposal_is_held_not_archived(tmp_path):
    """Snoozed proposals are NEVER archived by this sweep (2026-08-31).

    Reverses the original behavior deliberately. A snooze is an operator
    statement with its own timer — ``signals.stale_ttl`` already refuses
    to override a snoozed *Signal* for exactly this reason, and
    ``resolved_externally`` is terminal, so archiving a snoozed proposal
    put it somewhere ``arbiter.snooze_wake`` could never recover it.
    The signal here is genuinely resolved (not TTL-aged) — even that is
    not enough to overrule the snooze; it only defers to the wake."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:d"))
    p = make_investigation_proposal(bot_id="bot_w")
    transition(p, "pending", actor="test")
    p.motivating_signals = [s1.id]
    transition(p, "snoozed", actor="user")
    write_proposal(p, tmp_path)
    assert proposal_path(tmp_path, p.id, subdir="snoozed").exists()

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_skipped_snoozed == 1
    assert proposal_path(tmp_path, p.id, subdir="snoozed").exists()
    assert not proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_idempotent_when_run_twice(tmp_path):
    """Second sweep finds nothing to do — first sweep archived all
    eligible proposals."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:e"))
    p = _make_pending_proposal_with_signals(tmp_path, [s1.id])

    first = sweep_auto_resolve(tmp_path)
    second = sweep_auto_resolve(tmp_path)

    assert first.proposals_resolved == 1
    assert second.proposals_resolved == 0
    assert second.proposals_scanned == 0  # archived/ isn't iterated
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Tier3 staleness rule (2026-06-09)
# ─────────────────────────────────────────────────────────────────────────────


def _make_tier3_proposal(
    shared_dir: Path,
    *,
    age_days: float,
    bot_id: str = "bot_t3",
    generator_id: str = "app_audit_tier3",
    extra_history: bool = False,
):
    """Construct a pending proposal aged ``age_days`` ago.

    By default the proposal has exactly the draft→pending history
    entry (no engagement). ``extra_history=True`` appends a second
    transition (pending → snoozed → pending) to simulate operator
    engagement that should pin the proposal.
    """
    p = make_investigation_proposal(bot_id=bot_id, generator_id=generator_id)
    transition(p, "pending", actor="test")
    if extra_history:
        # Snooze + wake — two extra history entries beyond draft→pending.
        transition(p, "snoozed", actor="user")
        transition(p, "pending", actor="snooze_wake")
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    p.created_at = created.isoformat(timespec="seconds")
    write_proposal(p, shared_dir)
    return p


def test_tier3_archives_when_stale_and_unengaged(tmp_path):
    """A tier3 finding 8 days old with no engagement archives as
    resolved_externally with the tier3_staleness_7d reason."""
    p = _make_tier3_proposal(tmp_path, age_days=8.0)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 1
    assert result.proposals_resolved_tier3_stale == 1
    assert p.id in result.resolved_ids
    archived = proposal_path(tmp_path, p.id, subdir="archived")
    assert archived.exists()
    payload = json.loads(archived.read_text())
    assert payload["status"] == "resolved_externally"
    last = payload["history"][-1]
    assert last["to_status"] == "resolved_externally"
    assert last["actor"] == "auto_resolver"
    assert last["reason"] == "tier3_staleness_7d"


def test_tier3_skipped_when_recent(tmp_path):
    """A tier3 finding inside the 7-day window stays pending."""
    p = _make_tier3_proposal(tmp_path, age_days=3.0)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_resolved_tier3_stale == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_tier3_skipped_when_engaged(tmp_path):
    """An 8-day-old tier3 finding with operator engagement (extra
    history beyond draft→pending) stays pending — the operator
    touched it, so the staleness assumption no longer holds."""
    p = _make_tier3_proposal(tmp_path, age_days=8.0, extra_history=True)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_resolved_tier3_stale == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_non_tier3_proposal_not_archived_by_staleness(tmp_path):
    """Other generators have working dismiss flows — staleness rule
    must NOT touch them regardless of age."""
    p = _make_tier3_proposal(
        tmp_path, age_days=30.0, generator_id="efficiency_hawk"
    )

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved == 0
    assert result.proposals_resolved_tier3_stale == 0
    # No motivating_signals, so it falls into the no-signals skip path.
    assert result.proposals_skipped_no_signals == 1
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_tier3_staleness_at_exactly_7_days(tmp_path):
    """Boundary: at exactly 7 days the proposal archives (>=, not >)."""
    p = _make_tier3_proposal(tmp_path, age_days=7.001)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved_tier3_stale == 1
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_tier3_staleness_archives_regardless_of_signal_state(tmp_path):
    """The tier3 rule is independent of motivating-signal state — a
    stale tier3 finding archives even when its signals are still
    firing. The operator demonstrably is not engaging."""
    sig = _write_signal(
        tmp_path, _make_signal(state="firing", signature="t3:firing")
    )
    p = make_investigation_proposal(bot_id="bot_t3", generator_id="app_audit_tier3")
    transition(p, "pending", actor="test")
    p.motivating_signals = [sig.id]
    created = datetime.now(timezone.utc) - timedelta(days=10)
    p.created_at = created.isoformat(timespec="seconds")
    write_proposal(p, tmp_path)

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved_tier3_stale == 1
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


def test_tier3_now_override_is_honored(tmp_path):
    """The ``now`` kwarg lets callers pin the reference time — useful
    for tests but also lets the launchd wrapper add reproducible
    fixture-style runs in the future."""
    p = _make_tier3_proposal(tmp_path, age_days=3.0)

    # Six days in the future from the proposal's created_at puts it
    # at age 9d, past the staleness threshold.
    future = datetime.now(timezone.utc) + timedelta(days=6)
    result = sweep_auto_resolve(tmp_path, now=future)

    assert result.proposals_resolved_tier3_stale == 1
    assert proposal_path(tmp_path, p.id, subdir="archived").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Generic pending-proposal TTL (2026-08-30)
# ─────────────────────────────────────────────────────────────────────────────


def test_resolved_ttls_include_tier3_default_and_charter_optin():
    """The effective TTL map carries the charterless app_audit_tier3
    code default AND charter-declared opt-ins (engagement_amplifier's
    ``pending_ttl_days: 7``)."""
    from arbiter.auto_resolve import resolve_pending_ttls

    ttls = resolve_pending_ttls()
    assert ttls["app_audit_tier3"] == 7
    assert ttls["engagement_amplifier"] == 7


def test_charter_ttl_generator_archives_past_ttl(tmp_path):
    """A pending engagement_amplifier proposal 8 days old with no
    engagement archives via the charter-declared TTL, with the generic
    reason string."""
    p = _make_tier3_proposal(
        tmp_path, age_days=8.0, generator_id="engagement_amplifier"
    )

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved_ttl_stale == 1
    assert result.proposals_resolved_tier3_stale == 0
    archived = proposal_path(tmp_path, p.id, subdir="archived")
    assert archived.exists()
    payload = json.loads(archived.read_text())
    assert payload["status"] == "resolved_externally"
    assert payload["history"][-1]["reason"] == "pending_ttl_7d"


def test_no_ttl_generator_is_never_age_archived(tmp_path):
    """A generator with no TTL (neither code default nor charter
    opt-in) keeps its pending proposals regardless of age."""
    p = _make_tier3_proposal(
        tmp_path, age_days=90.0, generator_id="efficiency_hawk"
    )

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved_ttl_stale == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


def test_explicit_pending_ttls_override(tmp_path):
    """Callers can pass their own TTL map; it replaces the resolved
    defaults entirely."""
    p_hawk = _make_tier3_proposal(
        tmp_path, age_days=4.0, bot_id="bot_h", generator_id="efficiency_hawk"
    )
    p_t3 = _make_tier3_proposal(tmp_path, age_days=30.0, bot_id="bot_t3")

    result = sweep_auto_resolve(
        tmp_path, pending_ttls={"efficiency_hawk": 3}
    )

    # The override archives the hawk proposal and — because the map
    # replaces the defaults — leaves the ancient tier3 one alone.
    assert p_hawk.id in result.resolved_ids
    assert p_t3.id not in result.resolved_ids
    assert proposal_path(tmp_path, p_t3.id, subdir="pending").exists()


def test_dedup_refresh_advances_ttl_clock(tmp_path):
    """A machine dedup-refresh entry is a re-assertion, not operator
    engagement: it advances the staleness clock instead of pinning the
    proposal. Recent refresh → stays pending; stale refresh → archives."""
    from schema.proposal import StatusTransition

    def _with_refresh(bot_id: str, refresh_age_days: float):
        p = _make_tier3_proposal(
            tmp_path,
            age_days=30.0,
            bot_id=bot_id,
            generator_id="engagement_amplifier",
        )
        refreshed = datetime.now(timezone.utc) - timedelta(days=refresh_age_days)
        p.history.append(
            StatusTransition(
                from_status="pending",
                to_status="pending",
                at=refreshed.isoformat(timespec="seconds"),
                actor="arbiter",
                reason="dedup-refresh from prop-xyz",
            )
        )
        write_proposal(p, tmp_path, coalesce=False)
        return p

    p_live = _with_refresh("bot_live", refresh_age_days=1.0)
    p_stale = _with_refresh("bot_stale", refresh_age_days=10.0)

    result = sweep_auto_resolve(tmp_path)

    assert p_live.id not in result.resolved_ids
    assert proposal_path(tmp_path, p_live.id, subdir="pending").exists()
    assert p_stale.id in result.resolved_ids
    assert proposal_path(tmp_path, p_stale.id, subdir="archived").exists()


def test_operator_engagement_still_pins_despite_ttl(tmp_path):
    """Real operator history (snooze + wake) pins a TTL'd generator's
    proposal exactly as it pins tier3's."""
    p = _make_tier3_proposal(
        tmp_path,
        age_days=30.0,
        generator_id="engagement_amplifier",
        extra_history=True,
    )

    result = sweep_auto_resolve(tmp_path)

    assert result.proposals_resolved_ttl_stale == 0
    assert proposal_path(tmp_path, p.id, subdir="pending").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Failure isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_per_proposal_failure_does_not_abort_sweep(tmp_path, monkeypatch):
    """If one proposal hits an illegal transition or move OSError,
    sweep continues with the rest."""
    s1 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:f"))
    s2 = _write_signal(tmp_path, _make_signal(state="resolved", signature="s:g"))
    p_bad = _make_pending_proposal_with_signals(tmp_path, [s1.id])
    p_good = _make_pending_proposal_with_signals(tmp_path, [s2.id])

    # Simulate the bad proposal's move failing with OSError.
    from arbiter import auto_resolve as ar

    real_move = ar.move_proposal

    def flaky_move(proposal, shared_dir, *args, **kwargs):
        if proposal.id == p_bad.id:
            raise OSError("simulated EACCES on rename")
        return real_move(proposal, shared_dir, *args, **kwargs)

    monkeypatch.setattr(ar, "move_proposal", flaky_move)

    result = sweep_auto_resolve(tmp_path)

    # The good one still got archived, the bad one stayed pending.
    assert p_good.id in result.resolved_ids
    assert p_bad.id not in result.resolved_ids
    assert proposal_path(tmp_path, p_good.id, subdir="archived").exists()
    assert proposal_path(tmp_path, p_bad.id, subdir="pending").exists()
