"""Tests for pod_report v2 — three-bucket triage report.

The v2 report replaces the v1 6-section roll-up (each green/yellow/red
combined via _worst()) with three intent-shaped buckets that only show
when non-empty. Empty state is one line.

These tests pin the load-bearing contracts:
  - The report covers `yesterday`, never `today` (kills the 08:00 bug)
  - Empty state renders as a single line
  - Each Broken signal fires independently
  - Sustained-5-min guard keeps transient gateway races out of Broken
  - Trending uses per-bot baselines with sparse-bot suppression
  - Cold-start uses stricter factor and tags "(limited baseline)"
  - Top-3 grouping per metric; "+N more" suffix
  - Overall status maps from buckets correctly
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
from pod_report import (  # noqa: E402
    DEFAULT_OVERRIDES,
    ReportLine,
    collect_broken,
    collect_queue,
    collect_trending,
    render_report,
    run_report,
)


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


def _write_status(shared_dir: Path, bot_id: str, *, reachable: bool, age_seconds: float = 600):
    """Write a heal status file. age_seconds=600 → status is 10 min old (sustained)."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    payload = {
        "gateway_reachable": reachable,
        "gateway_running": reachable,
        "ts": ts.isoformat(),
    }
    (shared_dir / "status").mkdir(parents=True, exist_ok=True)
    (shared_dir / "status" / f"{bot_id}.json").write_text(json.dumps(payload))


def _seed_clean_baseline(shared_dir: Path, members: list[str], end_date: date,
                        sessions_mean: int = 30, cost_mean: float = 5.0,
                        days: int = 30):
    """Populate days of historical metrics for each bot."""
    for offset in range(1, days + 1):
        d = end_date - timedelta(days=offset)
        for bot_id in members:
            _write_metric(shared_dir, bot_id, d,
                          session_count=sessions_mean,
                          turn_count=sessions_mean * 2,
                          total_cost_estimated=cost_mean)


# ── Empty state ──


def test_empty_state_is_one_line(tmp_path: Path):
    """Clean pod with audit snapshot + non-anomalous metrics → one line."""
    members = ["team_bot_a", "team_bot_b"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)

    _seed_clean_baseline(tmp_path, members, end_date)
    # ref_date itself: a normal day inside baseline range, not anomalous
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=30, total_cost_estimated=5.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    _write_status(tmp_path, "team_bot_b", reachable=True)

    text, overall, _ = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Test",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    assert overall == "green"
    body_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("📊")]
    body_lines = [ln for ln in body_lines if ln.strip()]
    # 2026-06-05 consolidation: the "🟢 All clear · {empty_summary}"
    # placeholder was retired. The body now opens with a Pod usage
    # line containing yesterday's date and totals; quiet days have
    # only that line.
    assert len(body_lines) == 1, body_lines
    assert body_lines[0].startswith("Pod:"), body_lines[0]
    assert ref_date.isoformat() in body_lines[0]


def test_report_reads_yesterday_not_today(tmp_path: Path):
    """The 08:00 bug regression: 'today' must never be the report's reference date."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = date(2026, 5, 6)

    # Today (5/7) has a $1000 spend that should NEVER appear in the report
    # because measure.py for today hasn't run yet at 08:00. The report covers
    # yesterday (5/6). With no yesterday data, the empty-state should not show
    # today's bogus value.
    _write_metric(tmp_path, "team_bot_a", end_date, session_count=999, total_cost_estimated=1000.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    text, _, structured = run_report(
        tmp_path, members, DEFAULT_OVERRIDES, label="Daily",
        now=datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
    )

    assert structured["ref_date"] == ref_date.isoformat()
    assert "$1000" not in text
    assert "999" not in text


# ── Broken bucket ──


def test_audit_critical_findings_are_broken(tmp_path: Path):
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=14, warns=5)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    audit_lines = [l for l in broken if "Audit:" in l.text]
    assert len(audit_lines) == 1
    assert "14 CRITICAL" in audit_lines[0].text
    assert "5 warn" in audit_lines[0].text
    # Top-3 finding messages are inlined so the operator can triage from
    # the chat line without opening the UI (the May-2026 "counts only"
    # uselessness fix).
    assert "crit-0" in audit_lines[0].text
    assert "crit-1" in audit_lines[0].text
    assert "crit-2" in audit_lines[0].text
    assert "+11 more" in audit_lines[0].text
    assert audit_lines[0].severity == "red"


def test_audit_warns_alone_do_not_break(tmp_path: Path):
    """Warns are surfaced as part of the critical line. Warns alone shouldn't fire Broken."""
    members = ["team_bot_a"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _write_audit_snapshot(tmp_path, age_minutes=3, criticals=0, warns=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    audit_lines = [l for l in broken if "Audit" in l.text]
    assert audit_lines == []


def test_audit_stale_snapshot_is_broken_regardless_of_findings(tmp_path: Path):
    """Stale snapshot → red, even if it would have shown 'clean'."""
    members = ["team_bot_a"]
    ref_date = date(2026, 5, 6)
    _write_audit_snapshot(tmp_path, age_minutes=120, criticals=14)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    stale_lines = [l for l in broken if "stale" in l.text]
    assert len(stale_lines) == 1
    # Counts NOT reported when stale — the "N CRITICAL" / "N warn"
    # phrasing is the fresh-path's count format. (Bare-digit checks would
    # collide with digits in the embedded ISO timestamp.)
    assert "CRITICAL" not in stale_lines[0].text
    assert " warn" not in stale_lines[0].text


def test_audit_missing_snapshot_is_broken(tmp_path: Path):
    members = ["team_bot_a"]
    ref_date = date(2026, 5, 6)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    missing_lines = [l for l in broken if "no snapshot" in l.text]
    assert len(missing_lines) == 1


def test_gateway_unreachable_sustained_is_broken(tmp_path: Path):
    members = ["team_bot_a", "team_bot_b"]
    ref_date = date(2026, 5, 6)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    _write_status(tmp_path, "team_bot_b", reachable=False, age_seconds=600)  # 10 min old

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    live_lines = [l for l in broken if "Liveness" in l.text]
    assert len(live_lines) == 1
    assert "team_bot_b" in live_lines[0].text


def test_gateway_unreachable_transient_is_not_broken(tmp_path: Path):
    """The 5-min guard: a freshly-unreachable gateway must NOT fire (status race)."""
    members = ["team_bot_a"]
    ref_date = date(2026, 5, 6)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=False, age_seconds=60)  # 1 min old

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    live_lines = [l for l in broken if "Liveness" in l.text]
    assert live_lines == []


def test_pod_silent_is_broken_when_yesterday_zero(tmp_path: Path):
    members = ["team_bot_a", "team_bot_b"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_clean_baseline(tmp_path, members, end_date, sessions_mean=30)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=0, total_cost_estimated=0.0)
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=0, total_cost_estimated=0.0)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)
    _write_status(tmp_path, "team_bot_b", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    silent_lines = [l for l in broken if "Pod silent" in l.text]
    assert len(silent_lines) == 1
    assert "0 sessions across 2 bots" in silent_lines[0].text


def test_pod_silent_does_not_fire_when_no_data_yet(tmp_path: Path):
    """No metrics for ref_date at all → 'Metrics writer outage', not 'Pod silent'.

    Before Phase 6 of the alert-review hygiene, this case fell through to a
    silent return; now we emit the correct signal (writer outage). The
    legacy 'must not fire Pod silent' assertion still holds.
    """
    members = ["team_bot_a"]
    ref_date = date(2026, 5, 6)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    _write_status(tmp_path, "team_bot_a", reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    silent_lines = [l for l in broken if "Pod silent" in l.text]
    assert silent_lines == []
    outage_lines = [l for l in broken if l.signal_type == "metrics_outage"]
    assert len(outage_lines) == 1
    assert "0 of 1 bots reported" in outage_lines[0].text


def test_metrics_outage_fires_on_partial_coverage(tmp_path: Path):
    """When some bots reported and others didn't, distinguish writer outage
    from pod silence. Before Phase 6, partial coverage was mislabeled as
    'Pod silent' (observed on 2026-05-10 with 2 of 7 bots reporting)."""
    members = ["team_bot_a", "team_bot_b", "admin_bot"]
    ref_date = date(2026, 5, 10)
    # Only 2 of 3 bots have metrics — writer crashed mid-run.
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=5)
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=3)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    for b in members:
        _write_status(tmp_path, b, reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    outage = [l for l in broken if l.signal_type == "metrics_outage"]
    silent = [l for l in broken if l.signal_type == "pod_silent"]

    assert len(outage) == 1
    assert "only 2 of 3 bots reported" in outage[0].text
    assert "admin_bot" in outage[0].text  # the missing bot is named
    assert outage[0].meta and outage[0].meta["missing_bots"] == ["admin_bot"]
    assert outage[0].meta["reported_bots"] == ["team_bot_a", "team_bot_b"]
    # Crucial: don't ALSO emit pod_silent — partial coverage can't justify
    # claiming the pod is silent, even if the reported bots show 0 sessions.
    assert silent == []


def test_metrics_outage_fires_on_zero_coverage_with_known_members(tmp_path: Path):
    """No bot reported but the pod has members → writer outage, full message."""
    members = ["team_bot_a", "team_bot_b"]
    ref_date = date(2026, 5, 10)
    _write_audit_snapshot(tmp_path, age_minutes=3)
    for b in members:
        _write_status(tmp_path, b, reachable=True)

    broken = collect_broken(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    outage = [l for l in broken if l.signal_type == "metrics_outage"]
    assert len(outage) == 1
    assert "0 of 2 bots reported" in outage[0].text
    assert outage[0].meta["reported_bots"] == []
    assert outage[0].meta["missing_bots"] == ["team_bot_a", "team_bot_b"]


# ── Trending bucket ──


def test_cost_spike_per_bot(tmp_path: Path):
    members = ["team_bot_a", "team_bot_b"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_clean_baseline(tmp_path, members, end_date, sessions_mean=30, cost_mean=5.0)
    # team_bot_b spikes; team_bot_a stays normal
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=30, total_cost_estimated=25.0)
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=30, total_cost_estimated=5.0)

    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    cost_lines = [l for l in lines if "Cost spike" in l.text]
    assert len(cost_lines) == 1
    assert "team_bot_b" in cost_lines[0].text
    assert "team_bot_a" not in cost_lines[0].text  # team_bot_a not anomalous


def test_session_drop_per_bot(tmp_path: Path):
    members = ["team_bot_a", "team_bot_b"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_clean_baseline(tmp_path, members, end_date, sessions_mean=30)
    # team_bot_a drops; team_bot_b normal
    _write_metric(tmp_path, "team_bot_a", ref_date, session_count=2, total_cost_estimated=5.0)
    _write_metric(tmp_path, "team_bot_b", ref_date, session_count=30, total_cost_estimated=5.0)

    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    drop_lines = [l for l in lines if "Session drop" in l.text]
    assert len(drop_lines) == 1
    assert "team_bot_a" in drop_lines[0].text


def test_sparse_bot_suppression_for_cost(tmp_path: Path):
    """A bot whose 30d mean cost is below min_mean must not trend."""
    members = ["personal_bot"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    # Quiet bot: $0.10/day average
    _seed_clean_baseline(tmp_path, members, end_date, sessions_mean=1, cost_mean=0.10)
    _write_metric(tmp_path, "personal_bot", ref_date, session_count=1, total_cost_estimated=2.0)

    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    assert lines == []  # 20× ratio but base too small to be meaningful


def test_top_3_grouping_with_more_suffix(tmp_path: Path):
    """5 cost spikes → top 3 listed by ratio + '+2 more (...)' suffix."""
    members = ["a", "b", "c", "d", "e"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    _seed_clean_baseline(tmp_path, members, end_date, cost_mean=5.0)

    spikes = {"a": 50, "b": 40, "c": 30, "d": 20, "e": 15}
    for bot_id, cost in spikes.items():
        _write_metric(tmp_path, bot_id, ref_date,
                      session_count=30, total_cost_estimated=cost)

    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    cost_lines = [l for l in lines if "Cost spike" in l.text]
    assert len(cost_lines) == 1
    line = cost_lines[0].text
    # Top 3 by ratio (a, b, c) appear in head with $ amounts
    assert "a $50" in line
    assert "b $40" in line
    assert "c $30" in line
    # d and e appear only in the "+2 more" suffix
    assert "+2 more" in line
    assert "d, e" in line


def test_cold_start_uses_stricter_factor_and_tags_line(tmp_path: Path):
    """With <14 days of baseline, factor jumps to 3.0 and the line is tagged."""
    members = ["newbot"]
    end_date = date(2026, 5, 7)
    ref_date = end_date - timedelta(days=1)
    # Only 5 days of history — well under the 14-day threshold
    for offset in range(1, 6):
        _write_metric(tmp_path, "newbot", end_date - timedelta(days=offset),
                      session_count=10, total_cost_estimated=5.0)
    # Today: 12.0 — would be 2.4× (over normal factor 2.0) but under cold factor 3.0
    _write_metric(tmp_path, "newbot", ref_date,
                  session_count=10, total_cost_estimated=12.0)

    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    assert lines == []

    # Now a real cold-anomaly: 18.0 = 3.6× → fires, with cold tag
    _write_metric(tmp_path, "newbot", ref_date,
                  session_count=10, total_cost_estimated=18.0)
    lines = collect_trending(tmp_path, members, ref_date, DEFAULT_OVERRIDES)
    cost_lines = [l for l in lines if "Cost spike" in l.text]
    assert len(cost_lines) == 1
    assert "(limited baseline)" in cost_lines[0].text


# ── Queue bucket ──


def test_queue_blocked_tasks(tmp_path: Path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "pending.jsonl").write_text(
        '{"task_id": "t1", "status": "blocked"}\n'
        '{"task_id": "t2", "status": "pending"}\n'
        '{"task_id": "t3", "status": "blocked"}\n'
    )
    lines = collect_queue(tmp_path)
    blocked_lines = [l for l in lines if "blocked" in l.text]
    assert len(blocked_lines) == 1
    assert "2 blocked" in blocked_lines[0].text


def test_queue_empty_when_nothing_pending(tmp_path: Path):
    assert collect_queue(tmp_path) == []


# ── Render + overall ──


def test_render_orders_buckets_broken_trending_queue():
    broken = [ReportLine("broken", "red", "B1")]
    trending = [ReportLine("trending", "yellow", "T1")]
    queue = [ReportLine("queue", "info", "Q1")]
    text, overall = render_report("Test", broken, trending, queue, "summary")
    assert overall == "red"
    # Title is owned by the catalog body_template; render_report must
    # not emit a "📊 Pod Report — …" prefix or it'd be duplicated.
    assert "📊" not in text
    assert "Pod Report" not in text
    body = [l for l in text.splitlines() if l.strip()]
    assert body[0].startswith("🔴")
    assert body[1].startswith("⚠️")
    assert body[2].startswith("📋")


def test_overall_yellow_when_only_trending():
    text, overall = render_report(
        "Test", [], [ReportLine("trending", "yellow", "T1")], [], "summary",
    )
    assert overall == "yellow"


def test_overall_green_when_only_queue():
    """Queue is informational — never alerts. Pure-queue is green."""
    text, overall = render_report(
        "Test", [], [], [ReportLine("queue", "info", "Q1")], "summary",
    )
    assert overall == "green"


def test_overall_green_when_all_empty():
    # 2026-06-05 consolidation: the empty-state "🟢 All clear · {summary}"
    # line was retired. ``render_report`` returns an empty body when
    # no buckets fire AND no pod_usage_line is passed; ``overall``
    # still classifies as "green" for the admin UI's status pill.
    text, overall = render_report("Test", [], [], [], "summary")
    assert overall == "green"
    assert text == ""
    # Passing pod_usage_line makes that the entire body.
    text2, overall2 = render_report(
        "Test", [], [], [], "summary",
        pod_usage_line="Pod: 0 sessions, $0.00",
    )
    assert overall2 == "green"
    assert text2 == "Pod: 0 sessions, $0.00"
