"""Tests for the post-2026-06-04 structural cost detectors:

- ``detect_llm_workload_redundant_with_script``
- ``detect_heartbeat_cost_by_design``
- Their shared helpers: ``heartbeat_workload_chars``, ``_parse_every_to_hours``,
  ``_median_heartbeat_cost_usd``, ``_resolve_per_bot_cap``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import cost_watchdog  # noqa: E402


# ── heartbeat_workload_chars ────────────────────────────────────────────────


def test_workload_chars_zero_for_empty_or_none() -> None:
    assert cost_watchdog.heartbeat_workload_chars(None) == 0
    assert cost_watchdog.heartbeat_workload_chars("") == 0
    assert cost_watchdog.heartbeat_workload_chars("   \n\n\t\n") == 0


def test_workload_chars_skips_comment_lines() -> None:
    md = "# this is a comment\n# another\n\n   # indented hash\n"
    assert cost_watchdog.heartbeat_workload_chars(md) == 0


def test_workload_chars_counts_executable_content() -> None:
    md = "# comment\nrun a thing\n\n# more comment\nand do another\n"
    # "run a thing" (11) + "and do another" (14) = 25
    assert cost_watchdog.heartbeat_workload_chars(md) == 25


def test_workload_chars_inline_hash_inside_command_still_counts() -> None:
    md = "curl -H 'X: y' # not a comment line\n"
    # whole line is non-comment (doesn't start with #) → counted
    assert cost_watchdog.heartbeat_workload_chars(md) > 0


# ── _parse_every_to_hours ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2h", 2.0),
        ("4h", 4.0),
        ("30m", 0.5),
        ("90m", 1.5),
        ("1d", 24.0),
        ("3600", 1.0),
        ("3600s", 1.0),
    ],
)
def test_parse_every_to_hours_known_shapes(raw: str, expected: float) -> None:
    assert cost_watchdog._parse_every_to_hours(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "0s", "-1h", "junk"])
def test_parse_every_to_hours_invalid_returns_none(raw) -> None:
    assert cost_watchdog._parse_every_to_hours(raw) is None


# ── _median_heartbeat_cost_usd ──────────────────────────────────────────────


def _hb_event(session_id: str, cost: float) -> dict:
    return {
        "session_id": session_id,
        "source": "heartbeat",
        "channel": "heartbeat",
        "cost_usd": cost,
    }


def test_median_heartbeat_cost_aggregates_by_session() -> None:
    events = [
        _hb_event("s1", 0.10),
        _hb_event("s1", 0.05),  # same session — sums to 0.15
        _hb_event("s2", 0.08),
        _hb_event("s3", 0.20),
    ]
    median, sample = cost_watchdog._median_heartbeat_cost_usd(events)
    # session costs sorted: 0.08, 0.15, 0.20 → median 0.15
    assert sample == 3
    assert median == pytest.approx(0.15)


def test_median_heartbeat_cost_skips_non_heartbeat_sessions() -> None:
    events = [
        {"session_id": "u1", "source": "user", "channel": "telegram", "cost_usd": 0.50},
        _hb_event("h1", 0.10),
    ]
    median, sample = cost_watchdog._median_heartbeat_cost_usd(events)
    assert sample == 1
    assert median == pytest.approx(0.10)


def test_median_heartbeat_cost_empty_returns_zero() -> None:
    assert cost_watchdog._median_heartbeat_cost_usd([]) == (0.0, 0)


# ── _resolve_per_bot_cap ────────────────────────────────────────────────────


def test_resolve_per_bot_cap_reads_be_config(tmp_path) -> None:
    """Per-bot cap from better-engine-config (canonical, post-Phase 4)."""
    import json
    be_payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"security-bot": {"budget": {"per_bot_daily_hard_usd": 5.0}}},
    }
    (tmp_path / "better-engine-config.json").write_text(json.dumps(be_payload))
    assert cost_watchdog._resolve_per_bot_cap("security-bot", None, tmp_path) == 5.0


def test_resolve_per_bot_cap_falls_back_to_pod_global(tmp_path) -> None:
    """No per-bot value → pod-wide network.json::dailySpendCapUsd."""
    cfg = {"dailySpendCapUsd": 20.0}
    assert cost_watchdog._resolve_per_bot_cap(
        "security-bot", cfg, tmp_path
    ) == 20.0


def test_resolve_per_bot_cap_none_when_nothing_set(tmp_path) -> None:
    assert cost_watchdog._resolve_per_bot_cap("security-bot", {}, tmp_path) is None
    assert cost_watchdog._resolve_per_bot_cap("security-bot", None, tmp_path) is None


def test_resolve_per_bot_cap_none_when_be_value_zero(tmp_path) -> None:
    """Zero / negative BE-config value → treat as "no cap" (falls through
    to pod global, which is also unset here → None)."""
    import json
    be_payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"security-bot": {"budget": {"per_bot_daily_hard_usd": 0}}},
    }
    (tmp_path / "better-engine-config.json").write_text(json.dumps(be_payload))
    assert cost_watchdog._resolve_per_bot_cap("security-bot", None, tmp_path) is None


def test_resolve_per_bot_cap_ignores_legacy_network_json_per_bot(tmp_path) -> None:
    """Phase 4 removed the network.json::bots[bot].daily_cap_usd fallback.
    A value sitting there is silently ignored — operators must use BE
    config. (Pod-wide ``dailySpendCapUsd`` IS still consulted.)"""
    cfg = {"bots": {"security-bot": {"daily_cap_usd": 5.0}}}  # legacy per-bot
    # No BE config + no pod-wide global → no cap (was 5.0 pre-Phase-4)
    assert cost_watchdog._resolve_per_bot_cap("security-bot", cfg, tmp_path) is None


# ── detect_llm_workload_redundant_with_script ───────────────────────────────


def _oc_with_every(every: str | None) -> dict:
    hb: dict = {}
    if every is not None:
        hb["every"] = every
    return {"agents": {"defaults": {"heartbeat": hb}}}


def _hb_events_at(*costs: float) -> list[dict]:
    return [_hb_event(f"s{i}", c) for i, c in enumerate(costs)]


def test_llm_workload_redundant_fires_when_workload_and_cost_both_above_floor() -> None:
    """security-bot 2026-06-04 shape: 13KB workload + $0.13/hb at 30m → ~$6/day."""
    workload = "do this\n" * 200  # 1.6 KB of non-comment content
    oc = _oc_with_every("30m")
    events = _hb_events_at(0.10, 0.13, 0.15, 0.12, 0.14)  # median ~0.13
    out = cost_watchdog.detect_llm_workload_redundant_with_script(
        "security-bot",
        workload,
        oc,
        events,
        min_workload_chars=500,
        min_projected_daily_cost_usd=1.00,
        min_sessions_for_projection=3,
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "llm_workload_redundant_with_script"
    assert sig["severity"] == "warn"
    assert sig["details"]["projected_daily_cost_usd"] > 1.0
    # Sanity: at 30m cadence + ~$0.13/hb → ~$6.24/day
    assert sig["details"]["cadence_hours"] == pytest.approx(0.5)


def test_llm_workload_redundant_silent_when_heartbeat_md_is_comment_only() -> None:
    """The off-switch: comment-only HEARTBEAT.md → no fire even at high cost."""
    out = cost_watchdog.detect_llm_workload_redundant_with_script(
        "security-bot",
        "# coverage handed off to audit.py\n# all comments\n",
        _oc_with_every("30m"),
        _hb_events_at(1.00, 1.00, 1.00),
        min_workload_chars=500,
        min_projected_daily_cost_usd=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


def test_llm_workload_redundant_silent_when_projected_cost_below_floor() -> None:
    workload = "do this\n" * 200
    # 2h cadence + $0.01/hb → ~$0.12/day, below $1/day floor
    out = cost_watchdog.detect_llm_workload_redundant_with_script(
        "security-bot",
        workload,
        _oc_with_every("2h"),
        _hb_events_at(0.01, 0.01, 0.01, 0.01),
        min_workload_chars=500,
        min_projected_daily_cost_usd=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


def test_llm_workload_redundant_silent_when_sample_too_small() -> None:
    workload = "do this\n" * 200
    out = cost_watchdog.detect_llm_workload_redundant_with_script(
        "security-bot",
        workload,
        _oc_with_every("30m"),
        _hb_events_at(0.50),  # only 1 session — below sample floor
        min_workload_chars=500,
        min_projected_daily_cost_usd=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


def test_llm_workload_redundant_uses_30m_default_when_every_missing() -> None:
    """Operator left `every` unset — OC defaults to 30 min. The projection
    should still produce a usable number rather than silently skip."""
    workload = "do this\n" * 200
    out = cost_watchdog.detect_llm_workload_redundant_with_script(
        "security-bot",
        workload,
        _oc_with_every(None),  # heartbeat block exists but no `every`
        _hb_events_at(0.10, 0.13, 0.15),
        min_workload_chars=500,
        min_projected_daily_cost_usd=1.00,
        min_sessions_for_projection=3,
    )
    assert len(out) == 1
    assert out[0]["details"]["cadence_hours"] == 0.5


# ── detect_heartbeat_cost_by_design ─────────────────────────────────────────


def test_cost_by_design_warns_when_projection_above_warn_fraction() -> None:
    # 30m cadence + $0.10/hb → ~$4.80/day; cap $5 → 96% > 50% (warn) but < 100%
    out = cost_watchdog.detect_heartbeat_cost_by_design(
        "security-bot",
        _oc_with_every("30m"),
        _hb_events_at(0.10, 0.10, 0.10, 0.10),
        daily_cap_usd=5.0,
        warn_fraction=0.50,
        alert_fraction=1.00,
        min_sessions_for_projection=3,
    )
    assert len(out) == 1
    assert out[0]["severity"] == "warn"
    assert out[0]["details"]["cap_fraction"] >= 0.50


def test_cost_by_design_alerts_when_projection_meets_cap() -> None:
    # 30m + $0.15/hb → ~$7.20/day on $5 cap → 144% → alert
    out = cost_watchdog.detect_heartbeat_cost_by_design(
        "security-bot",
        _oc_with_every("30m"),
        _hb_events_at(0.15, 0.15, 0.15),
        daily_cap_usd=5.0,
        warn_fraction=0.50,
        alert_fraction=1.00,
        min_sessions_for_projection=3,
    )
    assert len(out) == 1
    assert out[0]["severity"] == "alert"


def test_cost_by_design_silent_when_cap_unset() -> None:
    out = cost_watchdog.detect_heartbeat_cost_by_design(
        "security-bot",
        _oc_with_every("30m"),
        _hb_events_at(1.0, 1.0, 1.0),
        daily_cap_usd=None,
        warn_fraction=0.50,
        alert_fraction=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


def test_cost_by_design_silent_below_warn_fraction() -> None:
    # 4h cadence + $0.10/hb → ~$0.60/day on $5 cap → 12% < 50%
    out = cost_watchdog.detect_heartbeat_cost_by_design(
        "security-bot",
        _oc_with_every("4h"),
        _hb_events_at(0.10, 0.10, 0.10, 0.10),
        daily_cap_usd=5.0,
        warn_fraction=0.50,
        alert_fraction=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


def test_cost_by_design_silent_when_sample_too_small() -> None:
    out = cost_watchdog.detect_heartbeat_cost_by_design(
        "security-bot",
        _oc_with_every("30m"),
        _hb_events_at(1.0),
        daily_cap_usd=5.0,
        warn_fraction=0.50,
        alert_fraction=1.00,
        min_sessions_for_projection=3,
    )
    assert out == []


# ── detect_heartbeat_cadence_anomaly ────────────────────────────────────────


def _hb_session_events(count: int) -> list[dict]:
    """Build N distinct heartbeat sessions with one event each."""
    return [_hb_event(f"hb-{i}", 0.10) for i in range(count)]


def _noon_utc() -> "datetime":
    from datetime import datetime, timezone

    return datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


def test_cadence_anomaly_fires_warn_at_1_5x_declared() -> None:
    """30 fires by noon at declared 2h cadence → projects to 60 vs expected 12."""
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("2h"),
        _hb_session_events(30),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "heartbeat_cadence_anomaly"
    assert sig["details"]["expected_fires_per_day"] == pytest.approx(12.0)
    assert sig["details"]["actual_fires_so_far"] == 30
    assert sig["details"]["projected_fires_24h"] > 12 * 1.5


def test_cadence_anomaly_alerts_at_3x_declared() -> None:
    """22 fires by noon at declared 2h → projects to 44 ≈ 3.7× expected 12."""
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("2h"),
        _hb_session_events(22),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert len(out) == 1
    assert out[0]["severity"] == "alert"


def test_cadence_anomaly_silent_when_within_tolerance() -> None:
    """6 fires by noon at 2h cadence → projects to 12 = expected. No fire."""
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("2h"),
        _hb_session_events(6),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert out == []


def test_cadence_anomaly_silent_when_extra_fires_below_floor() -> None:
    """Low-volume bot: 3 fires by noon at 8h cadence → projects to 6 vs
    expected 3 (2× ratio) but only 3 extra fires absolute → silent at
    min_extra_fires=6 floor."""
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("8h"),
        _hb_session_events(3),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert out == []


def test_cadence_anomaly_uses_30m_default_when_every_missing() -> None:
    """No `every` set → OC default 30m → expected 48/day. 50 fires by noon
    projects to 100 → 2× expected → warn."""
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every(None),
        _hb_session_events(50),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert len(out) == 1
    assert out[0]["details"]["cadence_hours"] == pytest.approx(0.5)


def test_cadence_anomaly_silent_when_no_heartbeats_today() -> None:
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("2h"),
        [],
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert out == []


def test_cadence_anomaly_silent_when_oc_json_missing() -> None:
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        None,
        _hb_session_events(30),
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    assert out == []


def test_cadence_anomaly_skips_non_heartbeat_sessions() -> None:
    """User sessions don't count toward the heartbeat actual."""
    events = (
        _hb_session_events(8)
        + [
            {"session_id": f"user-{i}", "source": "user",
             "channel": "telegram", "cost_usd": 0.10}
            for i in range(20)
        ]
    )
    out = cost_watchdog.detect_heartbeat_cadence_anomaly(
        "security-bot",
        _oc_with_every("2h"),
        events,
        factor=1.5,
        alert_factor=3.0,
        min_extra_fires=6,
        now=_noon_utc(),
    )
    # 8 hb-only by noon → projects to 16 → 1.33× < 1.5 → silent
    assert out == []
