"""tests/test_cost_watchdog_severity_tag.py — cost_watchdog retrofit.

Verifies (vector, magnitude) tags emitted on each detector's Signal kwargs
match the calibration in docs/spec-severity-framework-2026-05-18.md §2.2,
and that the severity framework's resolver reads them correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_watchdog as cw  # noqa: E402
import severity as sev  # noqa: E402


# ── _cost_magnitude_for_usd anchor table ─────────────────────────────────────


def test_cost_magnitude_anchors():
    assert cw._cost_magnitude_for_usd(0.50) == 0
    assert cw._cost_magnitude_for_usd(2.0) == 1
    assert cw._cost_magnitude_for_usd(10.0) == 2
    assert cw._cost_magnitude_for_usd(50.0) == 3
    assert cw._cost_magnitude_for_usd(150.0) == 4
    # Boundaries: thresholds use < so $1.0 → mag 1, $5.0 → mag 2.
    assert cw._cost_magnitude_for_usd(0.999) == 0
    assert cw._cost_magnitude_for_usd(1.0) == 1
    assert cw._cost_magnitude_for_usd(5.0) == 2
    assert cw._cost_magnitude_for_usd(25.0) == 3
    assert cw._cost_magnitude_for_usd(100.0) == 4


# ── detect_daily_spend ───────────────────────────────────────────────────────


def _spend_event(cost_usd: float, *, bot_id: str = "team_bot_a") -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": "2026-05-18T10:00:00Z",
        "bot_id": bot_id,
        "session_id": "s1",
        "trigger_kind": "user_turn",
        "model": "claude-sonnet",
        "provider": "anthropic",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": cost_usd,
    }


def test_daily_spend_magnitude_follows_dollar_amount():
    today = "2026-05-18"
    # $15 ≥ $5 threshold → mag 2 (cost: $5-25)
    out = cw.detect_daily_spend(
        "team_bot_a", [_spend_event(15.0)], threshold_usd=5.0, today=today
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 2
    assert out[0]["details"]["severity_active"] is True


def test_daily_spend_magnitude_4_at_100_plus():
    today = "2026-05-18"
    out = cw.detect_daily_spend(
        "team_bot_a", [_spend_event(120.0)], threshold_usd=5.0, today=today
    )
    assert out[0]["details"]["magnitude"] == 4


# ── detect_automation_dominance ──────────────────────────────────────────────


def test_automation_dominance_default_magnitude_1():
    events = []
    # 60% automation, 40% user → above 50% default threshold
    for _ in range(6):
        events.append(_spend_event(0.01))
        events[-1]["trigger_kind"] = "heartbeat"
    for _ in range(4):
        events.append(_spend_event(0.01))
    out = cw.detect_automation_dominance(
        "team_bot_a", events, ratio_threshold=0.5, min_turns=5, window_days=7
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 1


def test_automation_dominance_magnitude_2_when_extreme():
    """≥90% automation bumps to magnitude 2."""
    events = []
    for _ in range(9):
        e = _spend_event(0.01)
        e["trigger_kind"] = "heartbeat"
        events.append(e)
    events.append(_spend_event(0.01))  # 1 user turn
    out = cw.detect_automation_dominance(
        "team_bot_a", events, ratio_threshold=0.5, min_turns=5, window_days=7
    )
    assert out[0]["details"]["magnitude"] == 2


# ── detect_cron_overactive ───────────────────────────────────────────────────


def test_cron_overactive_magnitude_2_when_2x_expected(monkeypatch):
    cron = {
        "id": "c1",
        "name": "test",
        "enabled": True,
        "schedule": {"kind": "every", "everyMs": 3600_000},  # hourly
    }
    # 24h window, expected ~24 fires; provide 50 actual → ratio ~2.08
    monkeypatch.setattr(
        cw, "read_cron_runs",
        lambda *a, **kw: [{"action": "finished"}] * 50,
    )
    out = cw.detect_cron_overactive(
        "team_bot_a", [cron], factor=1.5, window_hours=24,
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 2
    assert out[0]["details"]["severity_active"] is True


# ── detect_context_bloat ─────────────────────────────────────────────────────


def test_context_bloat_magnitude_1_at_threshold():
    """At threshold (just over): magnitude 1."""
    sizes = {"USER.md": 40 * 1024}  # 40 KB, default threshold is 30 KB
    out = cw.detect_context_bloat("team_bot_a", sizes, dict(cw.DEFAULTS))
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 1


def test_context_bloat_magnitude_2_at_3x():
    sizes = {"USER.md": 100 * 1024}  # 100 KB vs 30 KB default → 3.33×
    out = cw.detect_context_bloat("team_bot_a", sizes, dict(cw.DEFAULTS))
    assert out[0]["details"]["magnitude"] == 2


# ── detect_cron_wakes_agent, detect_heartbeat_no_model_override ──────────────


def test_cron_wakes_agent_fixed_magnitude_1():
    cron = {
        "id": "c1",
        "name": "test",
        "enabled": True,
        "payload": {"kind": "systemEvent", "text": "echo hi"},
        "sessionTarget": "main",
        "wakeMode": "now",
        "schedule": {"kind": "every", "everyMs": 60_000},
    }
    out = cw.detect_cron_wakes_agent("team_bot_a", [cron])
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 1


def test_heartbeat_no_model_override_retired_emits_no_signals():
    """Retired 2026-06-04 — ModelRouter handles the override path.
    See detect_heartbeat_no_model_override docstring + retired-stub
    tests in test_cost_watchdog.py."""
    oc = {
        "agents": {
            "defaults": {
                "model": {"primary": "anthropic/claude-sonnet-4"},
                "heartbeat": {"every": "10m"},
            }
        }
    }
    assert cw.detect_heartbeat_no_model_override("team_bot_a", oc) == []


# ── detect_session_token_outlier ─────────────────────────────────────────────


def test_session_token_outlier_magnitude_follows_session_cost():
    """Magnitude follows the outlier session's absolute cost, not just
    its ratio to median. A $0.20 session at 5× a $0.04 median is still
    advisory; a $30 session at 5× a $6 median is real spend."""
    events: list[dict] = []
    # 3 baseline sessions @ $0.10 each, ~10 events each
    for s in ("s1", "s2", "s3"):
        for _ in range(10):
            e = _spend_event(0.01)
            e["session_id"] = s
            events.append(e)
    # Outlier session @ $30
    for _ in range(10):
        e = _spend_event(3.0)
        e["session_id"] = "s_big"
        events.append(e)
    out = cw.detect_session_token_outlier(
        "team_bot_a", events, factor=2.0, min_session_events=3,
        min_cost_usd=0.10, max_per_run=5,
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["details"]["vector"] == "cost"
    # $30 → magnitude 3 ($25-100 bucket)
    assert sig["details"]["magnitude"] == 3


# ── End-to-end resolver ──────────────────────────────────────────────────────


def test_resolver_picks_up_cost_tag():
    today = "2026-05-18"
    spec = cw.detect_daily_spend(
        "team_bot_a", [_spend_event(60.0)], threshold_usd=5.0, today=today
    )[0]
    rating = sev.resolve_severity(spec)
    assert rating.vector == "cost"
    assert rating.magnitude == 3
