"""tests/test_lineage.py — Proposal lineage by fingerprint."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.lineage import LineageIndex  # noqa: E402
from arbiter.store import write_proposal  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from generators.budget_hawk.proposals import (  # noqa: E402
    make_warn_pattern_investigation,
)


def _archive(p, *, status: str, shared_dir: Path):
    """Walk a proposal from draft → pending → terminal status and persist
    it under archived/ so the lineage walker picks it up. Each intermediate
    transition is legal per arbiter.state_machine._TRANSITIONS."""
    transition(p, "pending", actor="test")
    if status in (
        "succeeded",
        "failed_reverted",
        "failed_flagged",
        "failed_revert_failed",
    ):
        # pending → approved_human → applied → terminal
        transition(p, "approved_human", actor="user", reason="test")
        transition(p, "applied", actor="test")
        transition(p, status, actor="test")
    else:
        # rejected, dismissed, superseded, resolved_externally — all
        # reachable directly from pending.
        transition(p, status, actor="user", reason="test")
    write_proposal(p, shared_dir, subdir="archived")


def test_lineage_index_finds_same_fingerprint_history(tmp_path: Path):
    """Two past proposals with same fingerprint surface for a third."""
    p1 = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    p2 = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.5, cap_usd=2.0, observation_count=5
    )
    _archive(p1, status="dismissed", shared_dir=tmp_path)
    _archive(p2, status="dismissed", shared_dir=tmp_path)

    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=4.0, cap_usd=2.0, observation_count=6
    )
    idx = LineageIndex.build(tmp_path)
    entries = idx.lineage_for(candidate, max_entries=5, exclude_id=candidate.id)
    assert len(entries) == 2
    assert {e.proposal_id for e in entries} == {p1.id, p2.id}
    assert all(e.status == "dismissed" for e in entries)


def test_lineage_index_excludes_different_bot(tmp_path: Path):
    """A different bot has a different fingerprint — no false matches."""
    p_team_bot_c = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    _archive(p_team_bot_c, status="dismissed", shared_dir=tmp_path)

    candidate_team_bot_a = make_warn_pattern_investigation(
        "team_bot_a", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    idx = LineageIndex.build(tmp_path)
    entries = idx.lineage_for(candidate_team_bot_a)
    assert entries == []


def test_lineage_includes_succeeded_history(tmp_path: Path):
    """A previously succeeded proposal shows up in lineage — that's the
    'I approved this and verify confirmed it, but the issue is back' case."""
    p_old = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    _archive(p_old, status="succeeded", shared_dir=tmp_path)

    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    idx = LineageIndex.build(tmp_path)
    entries = idx.lineage_for(candidate, exclude_id=candidate.id)
    assert len(entries) == 1
    assert entries[0].status == "succeeded"
    assert entries[0].terminal_at is not None  # pulled from history


def test_lineage_returns_newest_first_and_caps_max_entries(tmp_path: Path):
    """When many past entries exist, lineage returns the most recent up
    to ``max_entries``."""
    for _ in range(7):
        p = make_warn_pattern_investigation(
            "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
        )
        _archive(p, status="dismissed", shared_dir=tmp_path)

    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    idx = LineageIndex.build(tmp_path)
    entries = idx.lineage_for(candidate, max_entries=3, exclude_id=candidate.id)
    assert len(entries) == 3
    # Newest first: terminal_at descending
    times = [e.terminal_at or "" for e in entries]
    assert times == sorted(times, reverse=True)


def test_lineage_empty_when_no_archive(tmp_path: Path):
    """Empty archive → empty index → empty lineage. Doesn't raise."""
    idx = LineageIndex.build(tmp_path)
    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4
    )
    assert idx.lineage_for(candidate) == []
