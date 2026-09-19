"""tests/test_efficiency_hawk_anomaly_surfaces.py — pin the
per-finding surface override on the six anomaly/maintenance factories
inside ``efficiency_hawk``.

Spec: internal/spec-rsi-proposal-eligibility-2026-06-05.md.

``efficiency_hawk`` declares ``surface: improvement`` at the charter
level — its RSI-shaped findings (cluster outliers, tier misrouting,
automation dominance) belong on Recommendations. But signal_proposals.py
also produces anomaly findings (single-session outliers, daily-spend
threshold trips, sys-admin nits) that should NOT land on Recommendations.

These factories override the charter by setting ``surface`` on the
emitted Proposal. A regression that drops the override re-routes
session_token_outlier (the screenshot finding that triggered this spec)
back to Recommendations.

Each row of the audit table that called for an override is pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.efficiency_hawk import signal_proposals as sp  # noqa: E402


class _FakeSignal:
    """Minimal stand-in for a Signal object — only the fields each
    factory reads. Direct attribute access mirrors what
    ``_signal_dict_get`` does in signal_proposals.py."""

    def __init__(self, *, bot_id: str = "team-bot-a", **details):
        self.id = "sig-test"
        self.bot_id = bot_id
        self.severity = "critical"
        self.first_seen_at = "2026-06-05T00:00:00Z"
        self.details = details


def test_session_token_outlier_surface_is_firing():
    """The screenshot finding. A single session that cost N× the
    median is anomaly triage, not RSI. Must route to Alerts."""
    sig = _FakeSignal(
        session_id="3d5cde22-1682-41a6-bdbd-962b9e627e06",
        cost_usd=7.67,
        median_session_cost_usd=0.25,
        ratio=30.8,
        event_count=33,
        trigger_kinds=["user_turn"],
        first_event_at="2026-05-29T00:52Z",
        last_event_at="2026-05-29T02:56Z",
    )
    p = sp.make_session_token_outlier_proposal(sig)
    assert p.surface == "firing", (
        "session_token_outlier must override charter.surface to "
        "'firing' — without it, the screenshot finding lands on "
        "Recommendations as it did pre-spec. "
        "See internal/spec-rsi-proposal-eligibility-2026-06-05.md."
    )


def test_daily_spend_high_surface_is_firing():
    """Daily spend crossing the configured threshold is an alert
    condition, not a Recommendation."""
    sig = _FakeSignal(
        cost_usd=5.0, threshold_usd=1.0, event_count=10, date="2026-06-05"
    )
    p = sp.make_daily_spend_proposal(sig)
    assert p.surface == "firing"


def test_context_bloat_surface_is_firing():
    """A memory file growing past threshold is hygiene/firefighting,
    not RSI."""
    sig = _FakeSignal(filename="memory.md", size_kb=100, threshold_kb=50)
    p = sp.make_context_bloat_proposal(sig)
    assert p.surface == "firing"


def test_cron_overactive_surface_is_drift():
    """A cron firing more often than configured is a sys-admin
    drift correction, not a capability change."""
    sig = _FakeSignal(
        cron_id="X",
        cron_name="X",
        actual_fires=14,
        expected_fires=1,
        window_hours=4,
        every_ms=60_000,
    )
    p = sp.make_cron_overactive_proposal(sig)
    assert p.surface == "drift"


def test_cron_wakes_agent_surface_is_drift():
    """A cron wrapped around an agent call is a config nit, not a
    Recommendation."""
    sig = _FakeSignal(
        cron_id="X",
        cron_name="X",
        cadence="15m",
        session_target="fresh",
        wake_mode="agent",
        shell="/bin/sh",
    )
    p = sp.make_cron_wakes_agent_proposal(sig)
    assert p.surface == "drift"


def test_heartbeat_no_model_override_surface_is_drift():
    """Set-this-knob config check — alert content, not RSI."""
    sig = _FakeSignal(
        primary_model="sonnet",
        heartbeat_every="5m",
        light_context=True,
        isolated_session=True,
    )
    p = sp.make_heartbeat_no_model_override_proposal(sig)
    assert p.surface == "drift"


def test_automation_dominance_keeps_charter_surface():
    """The RSI counter-example. ``automation_dominance`` IS a pattern
    proposal (background-trigger share over a multi-day window),
    routes to Recommendations. It must NOT override the charter —
    setting surface here would silently demote a real RSI finding."""
    sig = _FakeSignal(
        classified_share=0.85,
        background_share=0.7,
        distinct_days=7,
        total_cost_usd=5.0,
        lookback_days=7,
    )
    p = sp.make_automation_dominance_proposal(sig)
    assert p.surface is None, (
        "automation_dominance is RSI-shaped — pattern over a window, "
        "proposes rebalancing the bot's automation. It must inherit "
        "charter.surface=improvement, not override. A non-None "
        "surface here would route a real Recommendation off the "
        "Recommendations page."
    )
