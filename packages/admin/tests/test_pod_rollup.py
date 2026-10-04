"""Tests for pod_rollup — cross-bot aggregation widgets.

Covers:
  - compute_spend_rollup: per-bot cost + pod total + projected month-end
  - compute_attention_summary: signals + health chips
  - compute_activity_heat: ranked bots by turns
  - compute_upcoming_deadlines: scheduled apps from manifests
  - compute_pod_rollup: combined entry point
  - _describe_cron / _first_cron_schedule: schedule helpers
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent
_ADMIN_DIR = _TESTS_DIR.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"

for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.pod_rollup import (  # noqa: E402
    _describe_cron,
    _first_cron_schedule,
    compute_activity_heat,
    compute_attention_summary,
    compute_pod_rollup,
    compute_spend_rollup,
    compute_spend_rollup_live,
    compute_upcoming_deadlines,
)


# ── Fixture helpers ─────────────────────────────────────────────────────────


def _write_metrics(shared_dir: Path, bot_id: str, d: date, **fields) -> None:
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"bot_id": bot_id, "date": d.isoformat()}
    body.update(fields)
    p.write_text(json.dumps(body))


def _write_signal(shared_dir: Path, sig_id: str, bot_id: str | None = None) -> None:
    d = shared_dir / "signals" / "firing"
    d.mkdir(parents=True, exist_ok=True)
    # Fill the fields Signal.from_dict requires so the record loads through
    # the sanctioned signals.store read path (Phase B).
    sig = {
        "id": sig_id,
        "state": "firing",
        "signature": f"test_producer:test_type:{sig_id}",
        "producer": "test_producer",
        "type": "test_type",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": "bot" if bot_id else "pod",
    }
    if bot_id:
        sig["bot_id"] = bot_id
    (d / f"{sig_id}.json").write_text(json.dumps(sig))


def _write_manifest(
    shared_dir: Path, bot_id: str, app_id: str, **fields
) -> None:
    d = shared_dir / "applications" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"id": app_id, "name": app_id}
    manifest.update(fields)
    (d / f"{app_id}.json").write_text(json.dumps(manifest))


def _make_bot(role: str = "member", **tile_overrides) -> dict:
    """Return a minimal bot_data dict with an optional tile block."""
    tile = {
        "activity": {
            "turns_7d": 0, "turns_prior_7d": 0,
            "turns_28d": 0, "turns_prior_28d": 0,
            "sessions_7d": 0, "sessions_prior_7d": 0,
            "sessions_28d": 0, "sessions_prior_28d": 0,
            "human_pct_7d": None, "scheduled_pct_7d": None, "background_pct_7d": None,
            "human_pct_28d": None, "scheduled_pct_28d": None, "background_pct_28d": None,
        },
        "cost": {"usd_7d": 0.0, "usd_prior_7d": 0.0, "usd_28d": 0.0, "usd_prior_28d": 0.0},
        "apps": {"total": 0, "used_7d": 0, "used_28d": 0},
        "health_chips": [],
    }
    for k, v in tile_overrides.items():
        if k in tile and isinstance(tile[k], dict) and isinstance(v, dict):
            tile[k].update(v)
        else:
            tile[k] = v
    return {"role": role, "live": False, "last_metric_date": None, "tile": tile}


# ════════════════════════════════════════════════════════════════════════════
# _describe_cron / _first_cron_schedule
# ════════════════════════════════════════════════════════════════════════════


class TestDescribeCron:
    def test_daily_at_7am(self):
        assert _describe_cron("0 7 * * *") == "daily at 07:00"

    def test_daily_midnight(self):
        assert _describe_cron("0 0 * * *") == "daily at 00:00"

    def test_weekly_monday(self):
        assert _describe_cron("30 9 * * 1") == "weekly on Monday at 09:30"

    def test_monthly_on_day_1(self):
        assert _describe_cron("0 8 1 * *") == "monthly on day 1 at 08:00"

    def test_at_keyword_daily(self):
        assert _describe_cron("@daily") == "daily"

    def test_at_keyword_reboot(self):
        assert _describe_cron("@reboot") == "on reboot"

    def test_at_keyword_hourly(self):
        assert _describe_cron("@hourly") == "every hour"

    def test_unknown_exotic(self):
        # Falls back to raw string when schedule is non-standard
        result = _describe_cron("*/5 * * * *")
        assert "*/5" in result or result == "*/5 * * * *"

    def test_empty_string_returns_empty(self):
        assert _describe_cron("") == ""


class TestFirstCronSchedule:
    def test_raw_crontab_line_5fields(self):
        # "0 7 * * * /path/to/script" → "0 7 * * *"
        result = _first_cron_schedule(["0 7 * * * /path/script.py"])
        assert result == "0 7 * * *"

    def test_at_keyword_list_entry(self):
        result = _first_cron_schedule(["@daily /path/script.py"])
        assert result == "@daily"

    def test_dict_entry_with_schedule_key(self):
        result = _first_cron_schedule([{"schedule": "0 8 * * 1"}])
        assert result == "0 8 * * 1"

    def test_dict_entry_with_cron_string(self):
        result = _first_cron_schedule([{"cron_string": "30 6 * * *"}])
        assert result == "30 6 * * *"

    def test_empty_list_returns_none(self):
        assert _first_cron_schedule([]) is None

    def test_first_of_multiple(self):
        result = _first_cron_schedule(["0 7 * * *", "0 18 * * *"])
        assert result == "0 7 * * *"


# ════════════════════════════════════════════════════════════════════════════
# compute_spend_rollup
# ════════════════════════════════════════════════════════════════════════════


class TestComputeSpendRollup:
    def test_uses_tile_cost_when_present(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {
            "team_bot_a": _make_bot(cost={"usd_7d": 2.50, "usd_28d": 8.00}),
            "admin_bot": _make_bot(cost={"usd_7d": 1.00, "usd_28d": 4.00}),
        }
        result = compute_spend_rollup(tmp_path, bots, today=today)
        assert result["pod_total_7d"] == pytest.approx(3.50, abs=0.001)
        assert result["pod_total_28d"] == pytest.approx(12.00, abs=0.001)
        assert result["bots_with_spend_7d"] == 2
        assert result["currency"] == "USD"

    def test_pct_of_pod_sums_to_one(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {
            "team_bot_a": _make_bot(cost={"usd_7d": 1.0, "usd_28d": 6.0}),
            "admin_bot": _make_bot(cost={"usd_7d": 2.0, "usd_28d": 4.0}),
        }
        result = compute_spend_rollup(tmp_path, bots, today=today)
        pcts = [v["pct_of_pod"] for v in result["by_bot"].values() if v["pct_of_pod"] is not None]
        assert sum(pcts) == pytest.approx(1.0, abs=0.001)

    def test_fallback_reads_metrics_files(self, tmp_path):
        today = date(2026, 5, 12)
        # Bot with no tile — will fall back to metric file reads
        bot_data = {"role": "member", "live": False, "last_metric_date": None}
        _write_metrics(tmp_path, "team_bot_c", today, total_cost_estimated=1.5)
        _write_metrics(tmp_path, "team_bot_c", today - timedelta(days=1), total_cost_estimated=0.5)
        bots = {"team_bot_c": bot_data}
        result = compute_spend_rollup(tmp_path, bots, today=today)
        # 7d window includes both days → 2.0
        assert result["by_bot"]["team_bot_c"]["usd_7d"] == pytest.approx(2.0, abs=0.01)

    def test_projected_month_end_computed_midmonth(self, tmp_path):
        # Day 15 of a 31-day month; 28d rate should project to ~month spend
        today = date(2026, 5, 15)
        bots = {"team_bot_a": _make_bot(cost={"usd_7d": 0.0, "usd_28d": 14.0})}
        result = compute_spend_rollup(tmp_path, bots, today=today)
        # daily rate = 14/28 = 0.5; projected = 0.5 * 31 = 15.5
        assert result["projected_month_end"] == pytest.approx(15.5, abs=0.1)

    def test_projected_none_very_early_in_month(self, tmp_path):
        today = date(2026, 5, 2)  # day 2 — too early to project
        bots = {"team_bot_a": _make_bot(cost={"usd_7d": 0.0, "usd_28d": 10.0})}
        result = compute_spend_rollup(tmp_path, bots, today=today)
        assert result["projected_month_end"] is None

    def test_zero_spend_pod(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {"team_bot_a": _make_bot()}
        result = compute_spend_rollup(tmp_path, bots, today=today)
        assert result["pod_total_7d"] == 0.0
        assert result["bots_with_spend_7d"] == 0
        assert result["projected_month_end"] is None


# ════════════════════════════════════════════════════════════════════════════
# compute_spend_rollup_live — raw turn JSONL → per-day arrays + intraday
# ════════════════════════════════════════════════════════════════════════════


def _write_turns_jsonl(
    shared_dir: Path, bot_id: str, d: date, turns: list[dict],
) -> None:
    """Append ``turns`` (each becomes one JSONL line) to ``<bot>/turns/turns-<d>.jsonl``.

    Each turn dict is shallow-merged with a default ts of ``<d>T12:00:00Z``
    so the live aggregator's date-bucket extraction works without
    callers having to set ts on every fixture row.
    """
    td = shared_dir / bot_id / "turns"
    td.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in turns:
        row = {"ts": f"{d.isoformat()}T12:00:00Z", **t}
        rows.append(json.dumps(row))
    (td / f"turns-{d.isoformat()}.jsonl").write_text("\n".join(rows) + "\n")


class TestComputeSpendRollupLive:
    """Coverage for the live-JSONL path that backs ``pod_state.usage``.

    Anchored fixtures (``today=date(...)``) so the daily array is
    deterministic regardless of when the test runs.
    """

    def test_no_turn_dir_falls_back_to_lagged(self, tmp_path):
        """No bot has ``{shared_dir}/<bot>/turns/`` → wrapper falls back
        to the aggregate-based rollup, populates intraday + array fields
        as zeros/empty, and labels the source so callers can hedge."""
        today = date(2026, 5, 12)
        bots = {"team_bot_a": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)
        assert result["source"] == "lagged_metrics"
        assert result["pod_total_today"] == 0.0
        assert result["pod_total_yesterday"] == 0.0
        assert result["pod_daily_7d"] == []
        assert result["pod_daily_28d"] == []
        # Existing legacy fields still present
        assert result["pod_total_7d"] == 0.0
        assert result["currency"] == "USD"

    def test_today_intraday_from_live_jsonl(self, tmp_path):
        """A turn JSONL with a partial today + a full yesterday yields
        the right intraday today + yesterday + 7d/28d totals."""
        today = date(2026, 5, 12)
        yest = today - timedelta(days=1)
        _write_turns_jsonl(
            tmp_path, "team_bot_a", today,
            [{"cost": 0.50, "session_id": "s1"}, {"cost": 0.25, "session_id": "s1"}],
        )
        _write_turns_jsonl(
            tmp_path, "team_bot_a", yest,
            [{"cost": 1.00, "session_id": "s2"}, {"cost": 2.00, "session_id": "s3"}],
        )

        bots = {"team_bot_a": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)

        assert result["source"] == "live_jsonl"
        team_bot_a = result["by_bot"]["team_bot_a"]
        assert team_bot_a["usd_today"] == pytest.approx(0.75, abs=0.001)
        assert team_bot_a["usd_yesterday"] == pytest.approx(3.00, abs=0.001)
        assert team_bot_a["turns_today"] == 2
        assert team_bot_a["turns_yesterday"] == 2
        assert team_bot_a["usd_7d"] == pytest.approx(3.75, abs=0.001)
        assert team_bot_a["usd_28d"] == pytest.approx(3.75, abs=0.001)
        # daily_7d ends on today, second-to-last is yesterday
        assert team_bot_a["daily_7d"][-1]["date"] == today.isoformat()
        assert team_bot_a["daily_7d"][-1]["usd"] == pytest.approx(0.75, abs=0.001)
        assert team_bot_a["daily_7d"][-2]["date"] == yest.isoformat()
        assert team_bot_a["daily_7d"][-2]["usd"] == pytest.approx(3.00, abs=0.001)
        # And earlier days are zero-padded (no jsonl)
        assert team_bot_a["daily_7d"][0]["usd"] == 0.0

    def test_daily_arrays_28d_zero_padded(self, tmp_path):
        """daily_28d has exactly 28 entries; earlier days with no JSONL
        are zero-padded (so trend descriptions don't trip on gaps)."""
        today = date(2026, 5, 12)
        _write_turns_jsonl(
            tmp_path, "team_bot_a", today, [{"cost": 1.0, "session_id": "s1"}]
        )
        bots = {"team_bot_a": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)
        team_bot_a = result["by_bot"]["team_bot_a"]
        assert len(team_bot_a["daily_28d"]) == 28
        # Earliest day in the 28-day window
        expected_earliest = (today - timedelta(days=27)).isoformat()
        assert team_bot_a["daily_28d"][0]["date"] == expected_earliest
        assert team_bot_a["daily_28d"][0]["usd"] == 0.0
        # Latest day = today, has the one turn
        assert team_bot_a["daily_28d"][-1]["date"] == today.isoformat()
        assert team_bot_a["daily_28d"][-1]["usd"] == pytest.approx(1.0, abs=0.001)

    def test_spike_day_visible_in_array(self, tmp_path):
        """The whole point: a 4× day shows up as a distinct entry in
        daily_7d so evo can call it out by date, not just say the
        average is up. Mirrors the 2026-05-20 spike scenario."""
        today = date(2026, 5, 22)
        spike_day = today - timedelta(days=2)  # 2026-05-20
        # Baseline ~$8/day on a few neighboring days, spike at $33
        for offset, amount in [
            (6, 7.50),
            (5, 8.20),
            (4, 7.80),
            (3, 8.50),
            (2, 33.67),  # spike day
            (1, 8.00),
        ]:
            d = today - timedelta(days=offset)
            _write_turns_jsonl(
                tmp_path, "team_bot_a", d,
                [{"cost": amount, "session_id": f"s-{offset}"}],
            )
        _write_turns_jsonl(
            tmp_path, "team_bot_a", today, [{"cost": 2.50, "session_id": "s-today"}]
        )

        bots = {"team_bot_a": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)
        team_bot_a = result["by_bot"]["team_bot_a"]
        # Scan the array for the spike day — by date, not by index, so
        # this test is robust to off-by-one in the window construction.
        spike_entry = next(
            e for e in team_bot_a["daily_7d"] if e["date"] == spike_day.isoformat()
        )
        assert spike_entry["usd"] == pytest.approx(33.67, abs=0.01)
        # And the spike day's value is the max of the 7d window
        max_day = max(team_bot_a["daily_7d"], key=lambda e: e["usd"])
        assert max_day["date"] == spike_day.isoformat()

    def test_pod_totals_aggregate_across_bots(self, tmp_path):
        today = date(2026, 5, 12)
        _write_turns_jsonl(
            tmp_path, "team_bot_a", today, [{"cost": 1.00, "session_id": "k1"}]
        )
        _write_turns_jsonl(
            tmp_path, "admin_bot", today, [{"cost": 2.50, "session_id": "s1"}]
        )
        bots = {"team_bot_a": {"role": "member"}, "admin_bot": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)
        assert result["pod_total_today"] == pytest.approx(3.50, abs=0.001)
        # pod_daily_7d's last entry sums today's totals
        assert result["pod_daily_7d"][-1]["date"] == today.isoformat()
        assert result["pod_daily_7d"][-1]["usd"] == pytest.approx(3.50, abs=0.001)

    def test_pct_of_pod_uses_28d_denominator(self, tmp_path):
        today = date(2026, 5, 12)
        _write_turns_jsonl(
            tmp_path, "team_bot_a", today, [{"cost": 1.00, "session_id": "k1"}]
        )
        _write_turns_jsonl(
            tmp_path, "admin_bot", today, [{"cost": 3.00, "session_id": "s1"}]
        )
        bots = {"team_bot_a": {"role": "member"}, "admin_bot": {"role": "member"}}
        result = compute_spend_rollup_live(tmp_path, bots, today=today)
        team_bot_a_pct = result["by_bot"]["team_bot_a"]["pct_of_pod"]
        admin_bot_pct = result["by_bot"]["admin_bot"]["pct_of_pod"]
        assert team_bot_a_pct == pytest.approx(0.25, abs=0.001)
        assert admin_bot_pct == pytest.approx(0.75, abs=0.001)


# ════════════════════════════════════════════════════════════════════════════
# compute_attention_summary
# ════════════════════════════════════════════════════════════════════════════


class TestComputeAttentionSummary:
    def test_all_clear_when_no_signals_no_chips(self, tmp_path):
        bots = {"team_bot_a": _make_bot(), "admin_bot": _make_bot()}
        result = compute_attention_summary(tmp_path, bots)
        assert result["all_clear"] is True
        assert result["needs_attention"] == []
        assert result["total_firing_signals"] == 0

    def test_bot_with_critical_chip_appears(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(
                health_chips=[{"id": "gateway_down", "severity": "critical", "label": "gateway down"}]
            )
        }
        result = compute_attention_summary(tmp_path, bots)
        assert result["all_clear"] is False
        assert len(result["needs_attention"]) == 1
        assert result["needs_attention"][0]["bot_id"] == "team_bot_a"
        assert result["needs_attention"][0]["critical_chips"] == 1

    def test_firing_signals_counted(self, tmp_path):
        _write_signal(tmp_path, "sig-1", bot_id="admin_bot")
        _write_signal(tmp_path, "sig-2", bot_id="admin_bot")
        bots = {"admin_bot": _make_bot()}
        result = compute_attention_summary(tmp_path, bots)
        assert result["all_clear"] is False
        assert result["total_firing_signals"] == 2
        attn = result["needs_attention"][0]
        assert attn["bot_id"] == "admin_bot"
        assert attn["firing_signals"] == 2

    def test_sort_critical_first(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(
                health_chips=[{"id": "stale", "severity": "warn", "label": "stale heartbeat"}]
            ),
            "admin_bot": _make_bot(
                health_chips=[{"id": "gw_down", "severity": "critical", "label": "gateway down"}]
            ),
        }
        result = compute_attention_summary(tmp_path, bots)
        # admin_bot (critical) should come before team_bot_a (warn)
        assert result["needs_attention"][0]["bot_id"] == "admin_bot"

    def test_missing_signals_dir_handled(self, tmp_path):
        # No signals dir at all — should return all_clear (no crash)
        bots = {"team_bot_a": _make_bot()}
        result = compute_attention_summary(tmp_path, bots)
        assert result["all_clear"] is True

    def test_corrupt_signal_file_skipped(self, tmp_path):
        d = tmp_path / "signals" / "firing"
        d.mkdir(parents=True)
        (d / "bad.json").write_text("{not valid json")
        bots = {"team_bot_a": _make_bot()}
        result = compute_attention_summary(tmp_path, bots)
        assert result["all_clear"] is True  # bad file ignored

    def test_reasons_capped_at_five(self, tmp_path):
        chips = [
            {"id": f"chip-{i}", "severity": "warn", "label": f"warn {i}"}
            for i in range(7)
        ]
        bots = {"team_bot_a": _make_bot(health_chips=chips)}
        result = compute_attention_summary(tmp_path, bots)
        attn = result["needs_attention"][0]
        assert len(attn["reasons"]) <= 5


# ════════════════════════════════════════════════════════════════════════════
# compute_activity_heat
# ════════════════════════════════════════════════════════════════════════════


class TestComputeActivityHeat:
    def test_sorted_by_turns_descending(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(activity={"turns_7d": 10, "sessions_7d": 3}),
            "admin_bot": _make_bot(activity={"turns_7d": 80, "sessions_7d": 15}),
            "team_bot_b": _make_bot(activity={"turns_7d": 30, "sessions_7d": 7}),
        }
        result = compute_activity_heat(tmp_path, bots)
        ranked = result["ranked"]
        assert ranked[0]["bot_id"] == "admin_bot"
        assert ranked[1]["bot_id"] == "team_bot_b"
        assert ranked[2]["bot_id"] == "team_bot_a"

    def test_pod_totals_correct(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(activity={"turns_7d": 10, "sessions_7d": 3}),
            "admin_bot": _make_bot(activity={"turns_7d": 20, "sessions_7d": 5}),
        }
        result = compute_activity_heat(tmp_path, bots)
        assert result["pod_turns_7d"] == 30
        assert result["pod_sessions_7d"] == 8

    def test_busiest_bot_identified(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(activity={"turns_7d": 5, "sessions_7d": 1}),
            "admin_bot": _make_bot(activity={"turns_7d": 50, "sessions_7d": 10}),
        }
        result = compute_activity_heat(tmp_path, bots)
        assert result["busiest_bot"] == "admin_bot"

    def test_busiest_bot_none_when_no_activity(self, tmp_path):
        bots = {"team_bot_a": _make_bot(), "admin_bot": _make_bot()}
        result = compute_activity_heat(tmp_path, bots)
        assert result["busiest_bot"] is None

    def test_online_status_derived_from_live(self, tmp_path):
        bots = {
            "team_bot_a": dict(_make_bot(activity={"turns_7d": 5, "sessions_7d": 1}), live=True),
        }
        result = compute_activity_heat(tmp_path, bots)
        assert result["ranked"][0]["status"] == "online"

    def test_active_status_from_last_metric_date(self, tmp_path):
        bots = {
            "team_bot_a": dict(
                _make_bot(activity={"turns_7d": 5, "sessions_7d": 1}),
                live=False,
                last_metric_date="2026-05-11",
            ),
        }
        result = compute_activity_heat(tmp_path, bots)
        assert result["ranked"][0]["status"] == "active"

    def test_human_pct_included(self, tmp_path):
        bots = {
            "team_bot_a": _make_bot(activity={
                "turns_7d": 10, "sessions_7d": 3,
                "human_pct_7d": 0.6,
            }),
        }
        result = compute_activity_heat(tmp_path, bots)
        assert result["ranked"][0]["human_pct"] == pytest.approx(0.6)


# ════════════════════════════════════════════════════════════════════════════
# compute_upcoming_deadlines
# ════════════════════════════════════════════════════════════════════════════


class TestComputeUpcomingDeadlines:
    def test_scheduled_app_surfaced(self, tmp_path):
        _write_manifest(
            tmp_path, "admin_bot", "morning_brief",
            display_name="Morning Briefing",
            objective="Deliver a morning summary",
            crons=["0 7 * * * /path/brief.py"],
        )
        bots = {"admin_bot": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 1
        item = result["items"][0]
        assert item["bot_id"] == "admin_bot"
        assert item["app_id"] == "morning_brief"
        assert item["app_name"] == "Morning Briefing"
        assert item["schedule"] == "0 7 * * *"
        assert item["next_run_label"] == "daily at 07:00"

    def test_app_without_crons_not_surfaced(self, tmp_path):
        _write_manifest(tmp_path, "admin_bot", "ad_hoc", crons=[])
        bots = {"admin_bot": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 0

    def test_multiple_bots_aggregated(self, tmp_path):
        _write_manifest(tmp_path, "team_bot_a", "daily_report", crons=["0 9 * * 1"])
        _write_manifest(tmp_path, "admin_bot", "morning_brief", crons=["0 7 * * *"])
        bots = {"team_bot_a": _make_bot(), "admin_bot": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 2
        bot_ids = {item["bot_id"] for item in result["items"]}
        assert bot_ids == {"team_bot_a", "admin_bot"}

    def test_hidden_files_skipped(self, tmp_path):
        apps_dir = tmp_path / "applications" / "team_bot_a"
        apps_dir.mkdir(parents=True)
        (apps_dir / ".scan-status.json").write_text(json.dumps({"crons": ["0 1 * * *"]}))
        bots = {"team_bot_a": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 0

    def test_dict_cron_schedule(self, tmp_path):
        _write_manifest(
            tmp_path, "team_bot_a", "weekly_summary",
            crons=[{"schedule": "0 9 * * 1", "label": "weekly"}],
        )
        bots = {"team_bot_a": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 1
        assert result["items"][0]["schedule"] == "0 9 * * 1"

    def test_objective_truncated_at_120_chars(self, tmp_path):
        long_obj = "A" * 200
        _write_manifest(tmp_path, "team_bot_a", "app", crons=["0 7 * * *"], objective=long_obj)
        bots = {"team_bot_a": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert len(result["items"][0]["objective"]) == 120

    def test_corrupt_manifest_skipped(self, tmp_path):
        apps_dir = tmp_path / "applications" / "team_bot_a"
        apps_dir.mkdir(parents=True)
        (apps_dir / "bad.json").write_text("{not json")
        bots = {"team_bot_a": _make_bot()}
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 0

    def test_missing_applications_dir_handled(self, tmp_path):
        bots = {"team_bot_a": _make_bot()}  # no applications dir at all
        result = compute_upcoming_deadlines(tmp_path, bots)
        assert result["total"] == 0


# ════════════════════════════════════════════════════════════════════════════
# compute_pod_rollup (combined entry point)
# ════════════════════════════════════════════════════════════════════════════


class TestComputePodRollup:
    def test_returns_all_four_sections(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {
            "team_bot_a": _make_bot(cost={"usd_7d": 1.5, "usd_28d": 5.0}),
        }
        result = compute_pod_rollup(tmp_path, bots, today=today)
        assert set(result.keys()) == {"spend", "attention", "activity", "deadlines"}

    def test_spend_section_shape(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {"team_bot_a": _make_bot(cost={"usd_7d": 2.0, "usd_28d": 8.0})}
        result = compute_pod_rollup(tmp_path, bots, today=today)
        spend = result["spend"]
        assert "by_bot" in spend
        assert "pod_total_7d" in spend
        assert "pod_total_28d" in spend
        assert "currency" in spend

    def test_attention_section_shape(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {"team_bot_a": _make_bot()}
        result = compute_pod_rollup(tmp_path, bots, today=today)
        attn = result["attention"]
        assert "all_clear" in attn
        assert "needs_attention" in attn
        assert "total_firing_signals" in attn

    def test_activity_section_shape(self, tmp_path):
        today = date(2026, 5, 12)
        bots = {"team_bot_a": _make_bot(activity={"turns_7d": 5, "sessions_7d": 1})}
        result = compute_pod_rollup(tmp_path, bots, today=today)
        act = result["activity"]
        assert "ranked" in act
        assert "pod_turns_7d" in act
        assert "busiest_bot" in act

    def test_deadlines_section_shape(self, tmp_path):
        today = date(2026, 5, 12)
        _write_manifest(tmp_path, "team_bot_a", "brief", crons=["0 7 * * *"])
        bots = {"team_bot_a": _make_bot()}
        result = compute_pod_rollup(tmp_path, bots, today=today)
        dl = result["deadlines"]
        assert "items" in dl
        assert "total" in dl

    def test_empty_bots_dict_no_crash(self, tmp_path):
        today = date(2026, 5, 12)
        result = compute_pod_rollup(tmp_path, {}, today=today)
        assert result["spend"]["pod_total_7d"] == 0.0
        assert result["attention"]["all_clear"] is True
        assert result["activity"]["ranked"] == []
        assert result["deadlines"]["total"] == 0
