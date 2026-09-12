"""tests/test_proposal_synthesizer_budget.py — Budget tracker unit tests.

The Budget is a pure tracker — no I/O. These tests pin the cost
arithmetic and the soft/hard cap thresholds against the values in
the spec (§5.2).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from proposal_synthesizer.budget import (  # noqa: E402
    Budget,
    BudgetLimits,
    DEFAULT_INPUT_RATE_USD_PER_MTOK,
    DEFAULT_OUTPUT_RATE_USD_PER_MTOK,
    DEFAULT_LIMITS,
)


def test_default_limits_match_spec():
    """Spec §5.2 names exact numbers — don't drift."""
    L = DEFAULT_LIMITS
    assert L.soft_cost_usd_per_candidate == 0.50
    assert L.soft_turns_per_candidate == 10
    assert L.soft_cost_usd_per_run == 5.00
    assert L.hard_cost_usd_per_candidate == 2.00
    assert L.hard_turns_per_candidate == 25
    assert L.hard_cost_usd_per_run == 10.00
    assert L.hard_wall_seconds_per_candidate == 600.0
    assert L.hard_wall_seconds_per_run == 1800.0


def test_cost_arithmetic_uses_token_rates():
    b = Budget()
    b.record_turn(input_tokens=1_000_000, output_tokens=0)
    assert b.run.cost_usd == pytest.approx(DEFAULT_INPUT_RATE_USD_PER_MTOK)
    b2 = Budget()
    b2.record_turn(input_tokens=0, output_tokens=1_000_000)
    assert b2.run.cost_usd == pytest.approx(DEFAULT_OUTPUT_RATE_USD_PER_MTOK)


def test_continue_status_when_well_under_cap():
    b = Budget()
    b.record_turn(input_tokens=100, output_tokens=10)
    assert b.status() == "continue"


def test_soft_warning_at_soft_target():
    b = Budget()
    # Push cost above soft per-candidate ($0.50).
    b.record_turn(input_tokens=200_000, output_tokens=0)  # $0.60
    assert b.status() == "soft_warning"


def test_hard_cap_at_per_candidate_cost():
    b = Budget()
    b.record_turn(input_tokens=700_000, output_tokens=0)  # $2.10
    assert b.status() == "hard_cap"


def test_hard_cap_at_per_run_cost():
    """Per-run cap dominates even when current candidate is small."""
    L = BudgetLimits(hard_cost_usd_per_run=0.5, hard_cost_usd_per_candidate=100)
    b = Budget(limits=L)
    b.record_turn(input_tokens=200_000, output_tokens=0)  # $0.60 → over per-run cap
    assert b.status() == "hard_cap"


def test_start_candidate_resets_current_but_not_run():
    b = Budget()
    b.record_turn(input_tokens=100_000, output_tokens=0)
    run_cost_before = b.run.cost_usd
    b.start_candidate()
    assert b.current.turns == 0
    assert b.current.cost_usd == 0.0
    assert b.run.cost_usd == run_cost_before  # run total persists


def test_status_reason_names_the_dimension_at_cap():
    b = Budget()
    b.record_turn(input_tokens=700_000, output_tokens=0)
    reason = b.status_reason()
    assert "candidate-cost" in reason


def test_snapshot_includes_run_and_current():
    b = Budget()
    b.record_turn(input_tokens=1000, output_tokens=500)
    snap = b.snapshot()
    assert "run" in snap and "current_candidate" in snap
    assert snap["run"]["turns"] == 1
    assert snap["run"]["input_tokens"] == 1000
    assert snap["run"]["output_tokens"] == 500
