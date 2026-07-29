"""tests/test_rejection_log.py — Rejection log roundtrip + cooldown suppression."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.rejection_log import (  # noqa: E402
    read_recent_rejections,
    recent_rejection_fingerprints,
    rejection_log_path,
    write_rejection,
)
from better_engine_config import BetterEngineConfig  # noqa: E402
from generators.budget_hawk.observe import (  # noqa: E402
    BudgetHawkContext,
    observe as bh_observe,
)
from generators.budget_hawk.proposals import (  # noqa: E402
    make_warn_pattern_investigation,
)


def _config(warn=2.00, hard=5.00) -> BetterEngineConfig:
    return BetterEngineConfig.from_dict(
        {
            "schema_version": 1,
            "pod_defaults": {
                "better_engine": {"enabled": True},
                "rsi": {"enabled": True},
                "budget": {
                    "per_bot_daily_warn_usd": warn,
                    "per_bot_daily_hard_usd": hard,
                    "monthly_cap_usd": 50.0,
                },
            },
            "bots": {},
        }
    )


def _spend(today_usd: float):
    return lambda bot_id, days: [("2026-06-01", today_usd)]


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────────────
# Rejection log roundtrip
# ──────────────────────────────────────────────────────────────────────────


def test_rejection_log_roundtrip(tmp_path: Path):
    p = make_warn_pattern_investigation(
        "team_bot_c",
        current_usd=3.50,
        cap_usd=2.00,
        observation_count=4,
    )
    write_rejection(tmp_path, p, actor="user", reason="bot legitimately spends $3-4/day")

    log_path = rejection_log_path(tmp_path)
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["proposal_id"] == p.id
    assert entry["generator_id"] == "budget_hawk"
    assert entry["bot_id"] == "team_bot_c"
    assert entry["fingerprint"] == compute_fingerprint(p)
    assert entry["reason"] == "bot legitimately spends $3-4/day"
    assert entry["actor"] == "user"


def test_read_recent_filters_by_generator_and_bot(tmp_path: Path):
    p_team_bot_c = make_warn_pattern_investigation("team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4)
    p_team_bot_a = make_warn_pattern_investigation("team_bot_a", current_usd=3.0, cap_usd=2.0, observation_count=4)
    write_rejection(tmp_path, p_team_bot_c, actor="user")
    write_rejection(tmp_path, p_team_bot_a, actor="user")

    team_bot_c_only = read_recent_rejections(tmp_path, generator_id="budget_hawk", bot_id="team_bot_c")
    assert len(team_bot_c_only) == 1
    assert team_bot_c_only[0].bot_id == "team_bot_c"

    different_gen = read_recent_rejections(tmp_path, generator_id="other_gen")
    assert different_gen == []


def test_read_recent_drops_old_entries(tmp_path: Path):
    p = make_warn_pattern_investigation("team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4)
    write_rejection(tmp_path, p, actor="user")
    # Re-read with `now` shifted 30 days into the future and a 14-day window.
    future = datetime.now(timezone.utc) + timedelta(days=30)
    out = read_recent_rejections(
        tmp_path, generator_id="budget_hawk", within_days=14, now=future
    )
    assert out == []


def test_recent_fingerprints_helper_returns_set(tmp_path: Path):
    p = make_warn_pattern_investigation("team_bot_c", current_usd=3.0, cap_usd=2.0, observation_count=4)
    write_rejection(tmp_path, p, actor="user")
    fps = recent_rejection_fingerprints(tmp_path, generator_id="budget_hawk", bot_id="team_bot_c")
    assert isinstance(fps, set)
    assert compute_fingerprint(p) in fps


# ──────────────────────────────────────────────────────────────────────────
# Runner-level cooldown filter (applies uniformly to every generator)
# ──────────────────────────────────────────────────────────────────────────


def _build_runner_filter():
    """Reach into the runner module for the inlined cooldown filter logic.
    The filter is part of run_generators(); we exercise it here as a
    pure-function equivalent so we don't need a full runner harness."""
    from arbiter.dedup import compute_fingerprint as _fp
    from arbiter.rejection_log import recent_rejection_fingerprints as _rej_fps

    def filter_proposals(
        proposals,
        *,
        generator_id,
        shared_dir,
        cooldown_days=14,
        now=None,
    ):
        if not proposals:
            return list(proposals), 0
        blocked = _rej_fps(
            shared_dir,
            generator_id=generator_id,
            within_days=cooldown_days,
            now=now,
        )
        if not blocked:
            return list(proposals), 0
        kept, dropped = [], 0
        for p in proposals:
            if _fp(p) in blocked:
                dropped += 1
            else:
                kept.append(p)
        return kept, dropped

    return filter_proposals


def test_runner_cooldown_filter_drops_rejected_fingerprint(tmp_path: Path):
    """A proposal whose fingerprint is in the rejection log is dropped."""
    p = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    write_rejection(tmp_path, p, actor="user", reason="bot legitimately spends $3-4/day")
    filter_proposals = _build_runner_filter()
    # A fresh candidate with the same shape produces the same fingerprint.
    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.80, cap_usd=2.00, observation_count=5
    )
    kept, dropped = filter_proposals(
        [candidate], generator_id="budget_hawk", shared_dir=tmp_path
    )
    assert kept == []
    assert dropped == 1


def test_runner_cooldown_filter_does_not_drop_unrelated_fingerprint(tmp_path: Path):
    """A different proposal shape (different action / bot) survives."""
    rejected = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    write_rejection(tmp_path, rejected, actor="user")
    filter_proposals = _build_runner_filter()
    # Different bot → different fingerprint.
    candidate = make_warn_pattern_investigation(
        "team_bot_a", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    kept, dropped = filter_proposals(
        [candidate], generator_id="budget_hawk", shared_dir=tmp_path
    )
    assert len(kept) == 1
    assert dropped == 0


def test_runner_cooldown_filter_respects_window(tmp_path: Path):
    """Old rejections (outside the window) don't suppress new proposals."""
    p = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    write_rejection(tmp_path, p, actor="user")
    filter_proposals = _build_runner_filter()
    # Now is 30 days after the rejection; cooldown is 14 days.
    future = datetime.now(timezone.utc) + timedelta(days=30)
    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    kept, dropped = filter_proposals(
        [candidate],
        generator_id="budget_hawk",
        shared_dir=tmp_path,
        cooldown_days=14,
        now=future,
    )
    assert len(kept) == 1
    assert dropped == 0


def test_runner_cooldown_does_not_cross_generators(tmp_path: Path):
    """A budget_hawk rejection doesn't suppress an efficiency_hawk proposal
    even if the fingerprint somehow matched. Filter is generator-scoped."""
    p = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    write_rejection(tmp_path, p, actor="user")
    filter_proposals = _build_runner_filter()
    candidate = make_warn_pattern_investigation(
        "team_bot_c", current_usd=3.50, cap_usd=2.00, observation_count=4
    )
    # Querying as a different generator → no matches.
    kept, dropped = filter_proposals(
        [candidate],
        generator_id="efficiency_hawk",
        shared_dir=tmp_path,
    )
    assert len(kept) == 1
    assert dropped == 0
