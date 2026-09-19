"""tests/test_proposal_history.py — Phase 3 proposal_history lookup."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from investigation.proposal_history import (  # noqa: E402
    operator_already_declined,
    proposal_history,
    summarize_history,
)


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_proposal(
    shared_dir: Path,
    *,
    subdir: str,
    proposal_id: str,
    bot_id: str,
    generator_id: str = "bloat_investigator",
    cause_key: str = "growing_memory_drives_envelope",
    status: str = "dismissed",
    created_at: str = "2026-05-20T00:00:00+00:00",
    motivating_signals: list[str] | None = None,
) -> None:
    """Write a minimal proposal JSON file with the fields proposal_history
    reads. Bypasses Proposal.from_dict because we don't need all fields."""
    target_dir = shared_dir / "proposals" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": proposal_id,
        "bot_id": bot_id,
        "generator_id": generator_id,
        "dimension": "cost",
        "trigger_observations": [],
        "provenance": {
            "technique": f"bloat_investigator.{cause_key}",
            "signals": {
                "root_cause_attribution": {
                    "cause_key": cause_key,
                    "headline": "x",
                    "confidence": 0.9,
                    "primary_target": "f.md",
                    "evidence": {},
                },
            },
            "confidence": 0.9,
        },
        "problem": "p",
        "action": {"kind": "Investigation", "context": "c"},
        "risk_tag": {
            "blast_radius": "bot",
            "reversibility": "manual",
            "touches": [],
        },
        "claim": None,
        "revert_on_failure": None,
        "approval_audience": "pod_operator",
        "urgency": "improvement",
        "admin_surface_summary": "s",
        "conversational_pitch": None,
        "guardian_annotations": [],
        "conflicts_with": [],
        "status": status,
        "snoozed_until": None,
        "history": [],
        "revisions": [],
        "adjacency_type": None,
        "motivating_signals": motivating_signals or [],
        "schema_version": 1,
        "created_at": created_at,
        "signature": "",
    }
    (target_dir / f"{proposal_id}.json").write_text(json.dumps(payload))


# ── proposal_history ────────────────────────────────────────────────────────


def test_proposal_history_filters_by_bot_and_cause(tmp_path):
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="p1",
        bot_id="security_bot", status="dismissed",
        created_at="2026-05-20T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="p2",
        bot_id="security_bot",
        cause_key="static_bloat_drives_envelope",
        status="approved",
        created_at="2026-05-21T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="p3",
        bot_id="team_bot_a", status="dismissed",
        created_at="2026-05-22T00:00:00+00:00",
    )

    out = proposal_history(
        tmp_path,
        bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
    )
    assert [e.proposal_id for e in out] == ["p1"]


def test_proposal_history_sorts_newest_first(tmp_path):
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="old",
        bot_id="security_bot", created_at="2026-05-01T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="new",
        bot_id="security_bot", created_at="2026-05-20T00:00:00+00:00",
    )
    out = proposal_history(tmp_path, bot_id="security_bot")
    assert out[0].proposal_id == "new"
    assert out[1].proposal_id == "old"


def test_proposal_history_empty_when_no_proposals_dir(tmp_path):
    assert proposal_history(tmp_path, bot_id="security_bot") == []


def test_summarize_history_buckets_correctly(tmp_path):
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d1",
        bot_id="security_bot", status="dismissed",
        created_at="2026-05-19T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d2",
        bot_id="security_bot", status="rejected",
        created_at="2026-05-20T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="a1",
        bot_id="security_bot", status="applied",
        created_at="2026-05-21T00:00:00+00:00",
    )
    _write_proposal(
        tmp_path, subdir="pending", proposal_id="o1",
        bot_id="security_bot", status="pending",
        created_at="2026-05-22T00:00:00+00:00",
    )

    entries = proposal_history(
        tmp_path, bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
    )
    summary = summarize_history(
        entries, bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
    )
    assert summary.total == 4
    assert summary.declined == 2
    assert summary.approved == 1
    assert summary.open == 1
    assert summary.most_recent_status == "pending"


# ── operator_already_declined ──────────────────────────────────────────────


def test_operator_already_declined_threshold(tmp_path):
    # 2 dismissed -> hit
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d1",
        bot_id="security_bot", status="dismissed",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d2",
        bot_id="security_bot", status="dismissed",
        created_at="2026-05-21T00:00:00+00:00",
    )
    assert operator_already_declined(
        tmp_path, bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
        min_recent_declines=2,
    ) is True


def test_operator_already_declined_below_threshold(tmp_path):
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d1",
        bot_id="security_bot", status="dismissed",
    )
    # Only 1 decline; threshold is 2.
    assert operator_already_declined(
        tmp_path, bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
        min_recent_declines=2,
    ) is False


def test_operator_already_declined_ignores_other_causes(tmp_path):
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d1",
        bot_id="security_bot", status="dismissed",
        cause_key="static_bloat_drives_envelope",
    )
    _write_proposal(
        tmp_path, subdir="archived", proposal_id="d2",
        bot_id="security_bot", status="dismissed",
        cause_key="static_bloat_drives_envelope",
    )
    # Same bot, different cause — should not block.
    assert operator_already_declined(
        tmp_path, bot_id="security_bot",
        cause_key="growing_memory_drives_envelope",
    ) is False
