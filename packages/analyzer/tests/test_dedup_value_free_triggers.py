"""tests/test_dedup_value_free_triggers.py — trigger_observations must be
identity-only (value-free) so daily re-measurements refresh the open
proposal instead of minting a duplicate.

Regression for the live-queue incident where five near-duplicate
efficiency_hawk "Streamline atlas (creative-writing, exploring|researching)"
proposals coexisted in Pending: the measured turns/session ratio was baked
into trigger_observations, which feed the dedup fingerprint, so every daily
re-measurement produced a new fingerprint and a brand-new proposal. Same
churn shape for autonomy_promoter's growing streak count.

The dedup/merge machinery itself was always fine — ``ingest`` merges on
fingerprint collision via ``_refresh_existing``, which overwrites
provenance/claim/problem with the incoming (current) values. These tests pin
the generator-side contract: same cell + different measurement ⇒ same
fingerprint ⇒ merged, not duplicated.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.ingest import ingest  # noqa: E402
from generators.efficiency_hawk.observe import (  # noqa: E402
    EfficiencyHawkContext,
    _build_streamline_proposal,
)
from generators.autonomy_promoter.observe import _build_proposal  # noqa: E402
from observations.access import window  # noqa: E402


_DAY = datetime(2026, 5, 1, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Builders — real production builders, two different measurements per cell
# ─────────────────────────────────────────────────────────────────────────────


def _eh_proposal(tmp_path, *, ratio: float, mean: float, stdev: float,
                 noun: str = "atlas", verb: str = "exploring"):
    w = window(
        "team_bot_a",
        start=_DAY,
        end=_DAY + timedelta(days=1),
        shared_dir=tmp_path,
    )
    ctx = EfficiencyHawkContext(bot_id="team_bot_a", window=w)
    return _build_streamline_proposal(ctx, (noun, verb), ratio, mean, stdev)


class _FakeSignal:
    id = "sig-autonomy-1"
    bot_id = "team_bot_a"


class _FakePosture:
    kind = "email"


def _ap_proposal(*, actions: int, bot_id: str = "team_bot_a"):
    details = {
        "bot_id": bot_id,
        "integration_id": "google_workspace",
        "actions": actions,
        "span_days": 8,
        "suggested_actions_per_day": 6,
        "first_day": "2026-05-01",
        "last_day": "2026-05-08",
        "max_actions_per_day": 3,
    }
    p = _build_proposal(_FakeSignal(), _FakePosture(), details)
    assert p is not None
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint stability — same cell, different measurement ⇒ same fingerprint
# ─────────────────────────────────────────────────────────────────────────────


def test_efficiency_hawk_fingerprint_stable_across_measurements(tmp_path):
    # The live incident: 4.1 → 2.0 turns/session across daily runs of the
    # same (noun, verb) cell minted five coexisting proposals.
    p1 = _eh_proposal(tmp_path, ratio=4.1, mean=1.6, stdev=0.9)
    p2 = _eh_proposal(tmp_path, ratio=2.0, mean=1.4, stdev=0.4)
    assert compute_fingerprint(p1) == compute_fingerprint(p2)


def test_efficiency_hawk_fingerprint_distinguishes_cells(tmp_path):
    # Value-free must not mean cell-free: a different cluster is a
    # different proposal.
    p1 = _eh_proposal(tmp_path, ratio=4.1, mean=1.6, stdev=0.9)
    p2 = _eh_proposal(tmp_path, ratio=4.1, mean=1.6, stdev=0.9, verb="researching")
    assert compute_fingerprint(p1) != compute_fingerprint(p2)


def test_autonomy_promoter_fingerprint_stable_across_streak_growth():
    p1 = _ap_proposal(actions=12)
    p2 = _ap_proposal(actions=30)
    assert compute_fingerprint(p1) == compute_fingerprint(p2)


# ─────────────────────────────────────────────────────────────────────────────
# Ingest — re-detection merges into the open proposal, no new pending row
# ─────────────────────────────────────────────────────────────────────────────


def test_efficiency_hawk_redetection_merges_not_duplicates(tmp_path):
    first = _eh_proposal(tmp_path, ratio=4.1, mean=1.6, stdev=0.9)
    r1 = ingest(first)
    assert r1.accepted and r1.merged_into_id is None
    assert first.status == "pending"

    second = _eh_proposal(tmp_path, ratio=2.0, mean=1.4, stdev=0.4)
    r2 = ingest(second, open_proposals=[first])
    assert r2.merged_into_id == first.id

    # The open proposal's surface freshened to the current measurement.
    assert first.provenance.signals["engagement_per_session"] == 2.0
    assert first.claim is not None and first.claim.baseline == 2.0
    # Identity-only triggers: the merge adds nothing new.
    assert first.trigger_observations == ["cluster:atlas:exploring"]


def test_autonomy_promoter_redetection_merges_not_duplicates():
    first = _ap_proposal(actions=12)
    r1 = ingest(first)
    assert r1.accepted and r1.merged_into_id is None
    assert first.status == "pending"

    second = _ap_proposal(actions=30)
    r2 = ingest(second, open_proposals=[first])
    assert r2.merged_into_id == first.id

    assert first.provenance.signals["actions"] == 30
    assert first.trigger_observations == [
        "autonomy_promoter:streak:team_bot_a:google_workspace"
    ]
    # The Phase-A operator-facing prose freshens with the action: the
    # explanation the operator approves must describe the limits the
    # applier will actually set (consent match on a permission widening).
    assert first.summary is not None and "30 actions" in first.summary
    assert first.explanation is not None and "30" in first.explanation


def test_refresh_never_wipes_motivating_signals_with_empty():
    # Empty motivating_signals[] means "cannot reason about freshness" to
    # the auto-resolve sweep — a merge must not strand the open proposal
    # out of auto-resolution by clearing a non-empty list.
    first = _ap_proposal(actions=12)
    ingest(first)
    assert first.motivating_signals == ["sig-autonomy-1"]

    second = _ap_proposal(actions=30)
    second.motivating_signals = []
    r = ingest(second, open_proposals=[first])
    assert r.merged_into_id == first.id
    assert first.motivating_signals == ["sig-autonomy-1"]
