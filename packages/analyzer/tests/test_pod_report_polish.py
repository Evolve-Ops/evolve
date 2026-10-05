"""Tests for pod-report v2 polish: sparkline data, last-non-green anchor.

These pin the structured-output additions:
  - Trending lines carry meta.sparklines with per-bot 30d series for the
    head bots only (the bots actually named in the line text).
  - run_report's structured output includes last_non_green when a recent
    non-green report artifact exists in shared_dir/reports/.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pod_report  # noqa: E402
from pod_report import DEFAULT_OVERRIDES, run_report  # noqa: E402
from pod_report_baseline import history_for_bot  # noqa: E402


# ── Helpers ──


def _write_metric(shared_dir: Path, bot_id: str, d: date, **fields):
    folder = shared_dir / "metrics" / d.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 2, "bot_id": bot_id, "date": d.isoformat(), **fields}
    (folder / f"{bot_id}.json").write_text(json.dumps(payload))


def _write_audit_snapshot(shared_dir: Path, age_minutes: float, criticals: int = 0, warns: int = 0):
    completed_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    payload = {
        "schema_version": 1,
        "audit_completed_at": completed_at.isoformat(),
        "audit_succeeded": True,
        "critical": [{"category": "machine", "bot_id": None,
                      "message": f"crit-{i}", "detail": ""} for i in range(criticals)],
        "warn": [{"category": "config", "bot_id": "admin_bot",
                  "message": f"warn-{i}", "detail": ""} for i in range(warns)],
    }
    (shared_dir / "audit").mkdir(parents=True, exist_ok=True)
    (shared_dir / "audit" / "current-findings.json").write_text(json.dumps(payload))


def _write_status(shared_dir: Path, bot_id: str, *, reachable: bool):
    ts = datetime.now(timezone.utc)
    (shared_dir / "status").mkdir(parents=True, exist_ok=True)
    (shared_dir / "status" / f"{bot_id}.json").write_text(json.dumps({
        "gateway_reachable": reachable, "ts": ts.isoformat(),
    }))


def _seed_baseline(shared_dir, members, end_date, sessions_mean=30, cost_mean=5.0, days=30):
    for offset in range(1, days + 1):
        d = end_date - timedelta(days=offset)
        for bot_id in members:
            _write_metric(shared_dir, bot_id, d,
                          session_count=sessions_mean,
                          turn_count=sessions_mean * 2,
                          total_cost_estimated=cost_mean)


# ── history_for_bot ──


def test_history_returns_oldest_first(tmp_path: Path):
    end = date(2026, 5, 7)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=30)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 4), session_count=10)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 5), session_count=20)
    h = history_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert [v for _, v in h] == [10.0, 20.0, 30.0]
    assert [d for d, _ in h] == ["2026-05-04", "2026-05-05", "2026-05-06"]


def test_history_skips_missing_days(tmp_path: Path):
    end = date(2026, 5, 7)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=30)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 4), session_count=10)
    h = history_for_bot("session_count", "team_bot_b", tmp_path, end)
    # 2026-05-05 is missing — not in output (no zero-fill)
    assert len(h) == 2
    assert "2026-05-05" not in [d for d, _ in h]


def test_history_excludes_end_date(tmp_path: Path):
    end = date(2026, 5, 7)
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 7), session_count=999)  # excluded
    _write_metric(tmp_path, "team_bot_b", date(2026, 5, 6), session_count=30)
    h = history_for_bot("session_count", "team_bot_b", tmp_path, end)
    assert [v for _, v in h] == [30.0]


# ── Sparkline meta on trending lines ──


def test_cost_spike_line_includes_sparkline_meta(tmp_path: Path):
    members = ["team_bot_a", "team_bot_b"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_baseline(tmp_path, members, end_date, cost_mean=5.0)
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=30, total_cost_estimated=25.0)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    _write_status(tmp_path, "team_bot_b", reachable=True)

    _, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    trending = structured["buckets"]["trending"]
    cost_lines = [l for l in trending if "Cost spike" in l["text"]]
    assert len(cost_lines) == 1
    meta = cost_lines[0]["meta"]
    assert meta["sparklines"]["metric"] == "total_cost_estimated"
    series = meta["sparklines"]["series"]
    assert len(series) == 1  # only team_bot_b anomalous
    assert series[0]["bot_id"] == "team_bot_b"
    assert series[0]["current"] == 25.0
    # 30 days inclusive of ref_date — rightmost = current spike, prior 29 = baseline.
    # values[-1] == current is the load-bearing invariant for the sparkline UI.
    assert len(series[0]["values"]) == 30
    assert series[0]["values"][-1] == 25.0
    assert all(v == 5.0 for v in series[0]["values"][:-1])


def test_sparkline_only_for_head_bots_not_overflow(tmp_path: Path):
    """5 spikes → top 3 get series'd, the "+2 more" bots don't."""
    members = ["a", "b", "c", "d", "e"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_baseline(tmp_path, members, end_date, cost_mean=5.0)
    spikes = {"a": 50, "b": 40, "c": 30, "d": 20, "e": 15}
    for bot_id, cost in spikes.items():
        _write_metric(tmp_path, bot_id, ref_date,
                      session_count=30, total_cost_estimated=cost)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    for b in members:
        _write_status(tmp_path, b, reachable=True)

    _, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    trending = structured["buckets"]["trending"]
    cost_lines = [l for l in trending if "Cost spike" in l["text"]]
    series = cost_lines[0]["meta"]["sparklines"]["series"]
    # Only 3 head bots get sparklines (matching the named bots in the line)
    assert [s["bot_id"] for s in series] == ["a", "b", "c"]


def test_non_trending_lines_have_no_sparkline_meta(tmp_path: Path):
    """Broken and Queue bucket lines never carry sparkline payloads.

    Non-sparkline meta (e.g. ``pending_count``, ``affected_bots``) is
    allowed on these lines as of Phase 2 of the alerts/signal-store
    consolidation — sparklines specifically are a trending-bucket UI
    affordance.
    """
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=2)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    (tmp_path / "proposals" / "pending").mkdir(parents=True)
    (tmp_path / "proposals" / "pending" / "p1.json").write_text("{}")

    _, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    for line in structured["buckets"]["broken"] + structured["buckets"]["queue"]:
        meta = line.get("meta") or {}
        assert "sparklines" not in meta, f"non-trending line has sparklines: {line}"


# ── Last non-green anchor ──


def _write_report_artifact(shared_dir: Path, age_hours: float, overall: str, first_line: str = ""):
    """Write a fake reports/<ts>.json artifact."""
    out_dir = shared_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    fname = ts.strftime("%Y-%m-%dT%H-%M-%SZ") + "-test.json"
    body = f"📊 *Pod Report*\n\n{first_line}" if first_line else ""
    (out_dir / fname).write_text(json.dumps({
        "generated_at": ts.isoformat(timespec="seconds"),
        "label": "Daily",
        "overall": overall,
        "sent": True,
        "report_text": body,
        "schema_version": 2,
    }))


def test_last_non_green_returns_none_with_no_artifacts(tmp_path: Path):
    assert pod_report._find_last_non_green(tmp_path) is None


def test_last_non_green_returns_none_when_only_green(tmp_path: Path):
    _write_report_artifact(tmp_path, age_hours=24, overall="green")
    _write_report_artifact(tmp_path, age_hours=12, overall="green")
    assert pod_report._find_last_non_green(tmp_path) is None


def test_last_non_green_picks_most_recent_non_green(tmp_path: Path):
    _write_report_artifact(tmp_path, age_hours=72, overall="red", first_line="🔴 old")
    _write_report_artifact(tmp_path, age_hours=24, overall="yellow", first_line="⚠️ newer")
    _write_report_artifact(tmp_path, age_hours=6, overall="green")
    out = pod_report._find_last_non_green(tmp_path)
    assert out is not None
    assert out["overall"] == "yellow"
    assert "newer" in out["first_line"]


def test_last_non_green_window_filters_old(tmp_path: Path):
    """Outside max_age_days window → not returned."""
    _write_report_artifact(tmp_path, age_hours=24 * 30, overall="red")  # 30 days old
    assert pod_report._find_last_non_green(tmp_path, max_age_days=14) is None


def test_run_report_structured_includes_last_non_green(tmp_path: Path):
    """When a recent non-green artifact exists, run_report exposes it."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    _write_report_artifact(tmp_path, age_hours=18, overall="red", first_line="🔴 Audit: 14 open CRITICAL")

    _, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    assert structured["last_non_green"] is not None
    assert structured["last_non_green"]["overall"] == "red"
    assert "14 open CRITICAL" in structured["last_non_green"]["first_line"]


def test_run_report_structured_omits_last_non_green_when_clean(tmp_path: Path):
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_baseline(tmp_path, members, end_date)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    _, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )
    assert structured["last_non_green"] is None
