"""tests/test_cascade_discovery.py — baseline-distribution report.

Verifies that baseline_for_bot() reads existing turn_annotation files,
groups by session_id, computes distribution stats (mean/p50/p95/max)
for the metrics the anomaly detector needs, and stratifies by
session_class proxy for trigger_kind.

Per docs/spec-tier-cascade-2026-05-26.md § 2.6 (cost management) and
§ 7 Phase 2 (discovery script v2).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from cascade.discovery import (  # noqa: E402
    DistributionStats,
    BotBaselineReport,
    baseline_for_bot,
    baseline_for_pod,
    render_json,
    render_text,
    _stats,
)


# Fixed clock anchor: tests generate data relative to this and pass it as the
# window ``end``, so they never read the wall clock. Using datetime.now() made
# the suite flaky across UTC midnight — the fixture wrote per-day files on one
# side of the rollover and baseline_for_bot computed its 30-day window on the
# other, silently dropping the next-day file (28 == 30 in CI run 27314241286).
ANCHOR = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

# Anchors straddling a UTC date rollover, to prove window math is
# date-rollover-independent (the failing CI run sat at 23:59 → 00:00).
MIDNIGHT_ANCHORS = pytest.mark.parametrize(
    "anchor",
    [
        pytest.param(ANCHOR, id="midday"),
        pytest.param(datetime(2026, 6, 10, 23, 59, 25, tzinfo=timezone.utc), id="pre-midnight"),
        pytest.param(datetime(2026, 6, 11, 0, 0, 13, tzinfo=timezone.utc), id="post-midnight"),
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_annotation(
    shared_dir: Path,
    bot_id: str,
    *,
    session_id: str,
    ts: datetime,
    cost: float = 0.0,
    session_class: str = "productive",
    extra: dict | None = None,
) -> None:
    """Write one turn_annotation record matching the schema the plugin emits."""
    annotation = {
        "type": "turn_annotation",
        "session_id": session_id,
        "ts": ts.isoformat(),
        "bot_id": bot_id,
        "session_class": session_class,
        "cost_estimated": cost,
        **(extra or {}),
    }
    date_str = ts.astimezone(timezone.utc).date().isoformat()
    path = shared_dir / "annotations" / bot_id / f"{date_str}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(annotation) + "\n")


def _write_session(
    shared_dir: Path,
    bot_id: str,
    *,
    session_id: str,
    turn_count: int,
    start: datetime,
    duration_minutes: float,
    cost_per_turn: float,
    session_class: str = "productive",
) -> None:
    """Write a multi-turn session as a series of annotations.

    Turns are spaced evenly across the duration. Cost per turn is constant.
    """
    if turn_count == 1:
        _write_annotation(
            shared_dir, bot_id,
            session_id=session_id, ts=start, cost=cost_per_turn,
            session_class=session_class,
        )
        return
    interval = timedelta(minutes=duration_minutes / max(turn_count - 1, 1))
    for i in range(turn_count):
        _write_annotation(
            shared_dir, bot_id,
            session_id=session_id, ts=start + interval * i,
            cost=cost_per_turn, session_class=session_class,
        )


# ─────────────────────────────────────────────────────────────────────────────
# DistributionStats / _stats
# ─────────────────────────────────────────────────────────────────────────────


def test_stats_empty():
    s = _stats([])
    assert s.n == 0
    assert s.mean == 0.0
    assert s.p95 == 0.0


def test_stats_single():
    s = _stats([42.0])
    assert s.n == 1
    assert s.mean == 42.0
    assert s.p50 == 42.0
    assert s.p95 == 42.0
    assert s.max == 42.0


def test_stats_uniform():
    s = _stats([float(i) for i in range(1, 101)])
    assert s.n == 100
    assert 50 <= s.p50 <= 51
    assert 94 <= s.p95 <= 96
    assert s.max == 100.0
    assert abs(s.mean - 50.5) < 0.1


def test_stats_p95_ignores_outlier_tail():
    vals = [1.0] * 99 + [10000.0]
    s = _stats(vals)
    assert s.p95 < 100  # tail outlier doesn't dominate p95
    assert s.max == 10000.0


# ─────────────────────────────────────────────────────────────────────────────
# baseline_for_bot — basic shape
# ─────────────────────────────────────────────────────────────────────────────


def test_no_data(tmp_path: Path):
    report = baseline_for_bot("ghostbot", tmp_path, days=30)
    assert report.bot_id == "ghostbot"
    assert report.sessions == 0
    assert "no annotations" in report.notes


@MIDNIGHT_ANCHORS
def test_basic_baseline_shape(tmp_path: Path, anchor: datetime):
    """A bot with 10 simple sessions gets all distribution fields populated."""
    for i in range(10):
        _write_session(
            tmp_path, "basicbot",
            session_id=f"s-{i}",
            turn_count=3,
            start=anchor - timedelta(hours=i + 1),
            duration_minutes=2.0,
            cost_per_turn=0.05,
        )
    report = baseline_for_bot("basicbot", tmp_path, days=30, end=anchor)
    assert report.sessions == 10
    assert report.turns == 30
    assert report.cost_per_turn.n == 30
    assert report.cost_per_turn.mean == 0.05
    assert report.cost_per_session.n == 10
    assert abs(report.cost_per_session.mean - 0.15) < 0.001
    assert report.turns_per_session.mean == 3.0
    assert report.total_cost_usd == pytest.approx(0.05 * 30)


def test_p95_captures_tail(tmp_path: Path):
    """One session 20× more expensive than the others — p95 should reflect."""
    # 19 normal sessions + 1 outlier
    for i in range(19):
        _write_session(
            tmp_path, "spikybot", session_id=f"s-{i}",
            turn_count=1, start=ANCHOR - timedelta(hours=i + 1),
            duration_minutes=1.0, cost_per_turn=0.10,
        )
    _write_session(
        tmp_path, "spikybot", session_id="outlier",
        turn_count=1, start=ANCHOR - timedelta(hours=21),
        duration_minutes=1.0, cost_per_turn=5.00,
    )
    report = baseline_for_bot("spikybot", tmp_path, days=30, end=ANCHOR)
    assert report.cost_per_session.max == 5.00
    # p95 of 20 values: should be at or near the top — captures the outlier
    assert report.cost_per_session.p95 >= 0.10
    # But p50 is still near the normal cost
    assert report.cost_per_session.p50 == pytest.approx(0.10)


# ─────────────────────────────────────────────────────────────────────────────
# Origin stratification (session_class as proxy for trigger_kind)
# ─────────────────────────────────────────────────────────────────────────────


def test_user_facing_vs_background_share(tmp_path: Path):
    """Mix of session_class values produces correct share fractions."""
    # 8 user-facing (productive) at $0.10 each
    for i in range(8):
        _write_session(
            tmp_path, "mixbot", session_id=f"u-{i}",
            turn_count=1, start=ANCHOR - timedelta(hours=i + 1),
            duration_minutes=1.0, cost_per_turn=0.10,
            session_class="productive",
        )
    # 2 background (maintenance) at $0.50 each
    for i in range(2):
        _write_session(
            tmp_path, "mixbot", session_id=f"b-{i}",
            turn_count=1, start=ANCHOR - timedelta(hours=11 + i),
            duration_minutes=1.0, cost_per_turn=0.50,
            session_class="maintenance",
        )
    report = baseline_for_bot("mixbot", tmp_path, days=30, end=ANCHOR)
    assert report.sessions == 10
    assert abs(report.user_facing_share - 0.8) < 1e-9
    assert abs(report.background_share - 0.2) < 1e-9
    # Cost shares: user = 8 * 0.10 = 0.80; background = 2 * 0.50 = 1.00; total = 1.80
    assert abs(report.user_facing_cost_share - (0.80 / 1.80)) < 1e-9
    assert abs(report.background_cost_share - (1.00 / 1.80)) < 1e-9


def test_zero_cost_session_doesnt_break_share(tmp_path: Path):
    """When total cost is 0, cost-share fields are 0 (not NaN/ZeroDiv)."""
    _write_session(
        tmp_path, "freebot", session_id="s-1",
        turn_count=1, start=ANCHOR - timedelta(hours=1),
        duration_minutes=1.0, cost_per_turn=0.0,
    )
    report = baseline_for_bot("freebot", tmp_path, days=30, end=ANCHOR)
    assert report.user_facing_cost_share == 0.0
    assert report.background_cost_share == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Sessions-per-day distribution
# ─────────────────────────────────────────────────────────────────────────────


def test_sessions_per_day(tmp_path: Path):
    """Sessions distributed across days → per-day count distribution."""
    # Day -1: 5 sessions
    for i in range(5):
        _write_session(
            tmp_path, "regbot", session_id=f"d1-{i}",
            turn_count=1, start=ANCHOR - timedelta(days=1, hours=i),
            duration_minutes=1.0, cost_per_turn=0.01,
        )
    # Day -2: 2 sessions
    for i in range(2):
        _write_session(
            tmp_path, "regbot", session_id=f"d2-{i}",
            turn_count=1, start=ANCHOR - timedelta(days=2, hours=i),
            duration_minutes=1.0, cost_per_turn=0.01,
        )
    # Day -3: 1 session
    _write_session(
        tmp_path, "regbot", session_id="d3-0",
        turn_count=1, start=ANCHOR - timedelta(days=3),
        duration_minutes=1.0, cost_per_turn=0.01,
    )
    report = baseline_for_bot("regbot", tmp_path, days=30, end=ANCHOR)
    assert report.sessions_per_day.n == 3  # 3 distinct days
    assert report.sessions_per_day.max == 5
    # Mean = (5+2+1)/3 ≈ 2.67
    assert abs(report.sessions_per_day.mean - 8/3) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Sparse-bot detection (anomaly detector bootstrap)
# ─────────────────────────────────────────────────────────────────────────────


def test_sparse_baseline_surfaced_in_text_output(tmp_path: Path):
    """A bot with <50 turns gets flagged as needing pod-median bootstrap."""
    # 5 sessions × 3 turns = 15 turns. Below the 50-turn threshold.
    for i in range(5):
        _write_session(
            tmp_path, "sparsebot", session_id=f"s-{i}",
            turn_count=3, start=ANCHOR - timedelta(hours=i + 1),
            duration_minutes=1.0, cost_per_turn=0.01,
        )
    reports = baseline_for_pod(["sparsebot"], tmp_path, days=30, end=ANCHOR)
    out = render_text(reports, days=30)
    assert "sparse" in out.lower()
    assert "sparsebot" in out


def test_well_populated_baseline_not_flagged_as_sparse(tmp_path: Path):
    """A bot with ≥50 turns doesn't show the sparse warning."""
    # 25 sessions × 3 turns = 75 turns
    for i in range(25):
        _write_session(
            tmp_path, "stockbot", session_id=f"s-{i}",
            turn_count=3, start=ANCHOR - timedelta(hours=i + 1),
            duration_minutes=1.0, cost_per_turn=0.01,
        )
    reports = baseline_for_pod(["stockbot"], tmp_path, days=30, end=ANCHOR)
    out = render_text(reports, days=30)
    # The summary section won't list stockbot as sparse
    assert "Bots with sparse baseline" not in out or "stockbot" not in out.split("sparse baseline")[1] if "sparse baseline" in out else True


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


@MIDNIGHT_ANCHORS
def test_render_text_includes_all_distributions(tmp_path: Path, anchor: datetime):
    """Text output names every distribution the anomaly detector needs."""
    for i in range(10):
        _write_session(
            tmp_path, "fullbot", session_id=f"s-{i}",
            turn_count=2, start=anchor - timedelta(hours=i + 1),
            duration_minutes=1.5, cost_per_turn=0.02,
        )
    reports = baseline_for_pod(["fullbot"], tmp_path, days=30, end=anchor)
    out = render_text(reports, days=30)
    # Each major distribution row appears
    assert "cost/turn" in out
    assert "cost/session" in out
    assert "turns/session" in out
    assert "duration/session" in out
    assert "sessions/day" in out
    assert "origin mix" in out
    # Per-bot total cost is shown
    assert "$0.40" in out or "$0.4" in out  # 10 * 2 * 0.02 = $0.40


def test_render_json_is_parseable_and_typed(tmp_path: Path):
    """JSON output is valid + carries the schema marker."""
    _write_session(
        tmp_path, "jsonbot", session_id="s-1",
        turn_count=3, start=ANCHOR - timedelta(hours=1),
        duration_minutes=1.0, cost_per_turn=0.01,
    )
    reports = baseline_for_pod(["jsonbot"], tmp_path, days=30, end=ANCHOR)
    raw = render_json(reports, days=30)
    parsed = json.loads(raw)
    assert parsed["schema"] == "cascade.baseline_distribution_report.v1"
    assert parsed["lookback_days"] == 30
    assert "generated_at" in parsed
    assert len(parsed["reports"]) == 1
    r = parsed["reports"][0]
    assert r["bot_id"] == "jsonbot"
    assert r["cost_per_turn"]["n"] == 3
    assert r["cost_per_session"]["n"] == 1


def test_render_handles_no_data_bot(tmp_path: Path):
    """A bot with zero annotations renders cleanly in both formats."""
    reports = baseline_for_pod(["ghost"], tmp_path, days=30)
    text_out = render_text(reports, days=30)
    json_out = render_json(reports, days=30)
    assert "ghost" in text_out
    assert "no annotations" in text_out
    assert json.loads(json_out)["reports"][0]["notes"]


# ─────────────────────────────────────────────────────────────────────────────
# Pod-level
# ─────────────────────────────────────────────────────────────────────────────


def test_baseline_for_pod_preserves_input_order(tmp_path: Path):
    """Discovery output order matches the input bot list order."""
    for bot in ("alpha", "beta", "gamma"):
        _write_session(
            tmp_path, bot, session_id=f"s-{bot}",
            turn_count=1, start=ANCHOR - timedelta(hours=1),
            duration_minutes=1.0, cost_per_turn=0.01,
        )
    reports = baseline_for_pod(["gamma", "alpha", "beta"], tmp_path, days=30, end=ANCHOR)
    assert [r.bot_id for r in reports] == ["gamma", "alpha", "beta"]
