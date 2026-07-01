"""Cost-to-first-value projection — the source-of-truth contract.

The add-bot wizard's "cost-to-first-value" panel (META:user-value, Wave 3)
shows a brand-new bot's projected cost envelope at creation, *before* it's
provisioned. The whole point is that the displayed figures come from the same
resolvers the enforcers use, so the panel can never drift from what's actually
enforced — there are no dollar literals on the UI side. The envelope is
assembled from two sources, each owned by its enforcer:

  * the cap fields (provisioning ceiling + graduated/pod daily hard caps +
    window) — ``BetterEngineConfig.new_bot_cost_projection`` (BE config);
  * the daily-spend alert — ``spend_alert.daily_spend_alert_threshold_usd``
    (network.json), the value the spend alerter actually fires on.

These tests pin the no-drift contract for both halves: each projected figure
is cross-checked against the resolver that *enforces* it (an age-0 bot routed
through the real ``budget_hard_cap_usd`` for the in-window cap; the spend
alerter's own threshold accessor for the alert), not merely against a literal.

Finding: docs/finding-new-bot-activation-cost-2026-06-12.md (decisions A + B).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import spend_alert  # noqa: E402
from better_engine_config import (  # noqa: E402
    NEW_BOT_DAILY_HARD_USD,
    NEW_BOT_GRADUATION_DAYS,
    NEW_BOT_PROVISIONING_CEILING_USD,
    BetterEngineConfig,
)

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


# ─── Cap fields: projection mirrors the real enforcement resolvers ──────────


def test_cap_fields_match_enforcement_resolvers():
    """Every projected cap equals what the enforcer would resolve.

    A brand-new bot has no per-bot overrides, so its in-window hard cap is the
    graduated default the daily-cap enforcer applies, its post-window cap is the
    pod default, its ceiling is what ``provisioning_budget`` enforces. If any of
    these drifts, the wizard would promise a number the system doesn't enforce.
    """
    cfg = BetterEngineConfig.default()
    proj = cfg.new_bot_cost_projection()

    # One-time provisioning ceiling — same source provisioning_budget reads.
    assert proj["provisioning_ceiling_usd"] == cfg.provisioning_ceiling_usd(None)

    # In-window daily hard cap == the graduated default the enforcer returns
    # for an age-0 bot (set created_at = now and resolve through the real
    # budget_hard_cap_usd path to prove equivalence, not just constant reuse).
    cfg_new = BetterEngineConfig.default()
    cfg_new.set_bot_created_at("freshbot", _NOW)
    assert proj["daily_hard_cap_usd"] == cfg_new.budget_hard_cap_usd(
        "freshbot", now=_NOW
    )

    # Post-window daily hard cap == pod default (a mature bot with no override).
    assert proj["daily_hard_cap_after_window_usd"] == cfg.resolve(
        None, "budget", "per_bot_daily_hard_usd"
    )

    # Window == the graduation window constant.
    assert proj["window_days"] == float(NEW_BOT_GRADUATION_DAYS)

    # The alert is NOT a BE-config field — it's layered on from spend_alert.
    assert "daily_alert_usd" not in proj


def test_cap_compiled_default_values():
    """The cap numbers a fresh pod's wizard shows (compiled product defaults)."""
    proj = BetterEngineConfig.default().new_bot_cost_projection()
    assert proj["provisioning_ceiling_usd"] == 12.00
    assert proj["daily_hard_cap_usd"] == 10.00
    assert proj["daily_hard_cap_after_window_usd"] == 5.00
    assert proj["window_days"] == 7.0
    # Sanity: the constants are wired, not re-typed.
    assert proj["provisioning_ceiling_usd"] == NEW_BOT_PROVISIONING_CEILING_USD
    assert proj["daily_hard_cap_usd"] == NEW_BOT_DAILY_HARD_USD


def test_pod_hard_cap_override_flows_through_without_drift():
    """A pod that raises the daily hard-cap default is reflected, not ignored.

    The projection must surface whatever the pod configured so the panel matches
    enforcement, never a baked-in figure.
    """
    cfg = BetterEngineConfig.default()
    cfg.pod_defaults.setdefault("budget", {})["per_bot_daily_hard_usd"] = 20.00
    proj = cfg.new_bot_cost_projection()
    assert proj["daily_hard_cap_after_window_usd"] == 20.00
    # In-window hard cap is the graduated default — unaffected by the pod
    # default change (the new-bot tier sits above the pod default).
    assert proj["daily_hard_cap_usd"] == 10.00


# ─── Alert field: same threshold the spend alerter fires on ─────────────────


def test_alert_threshold_compiled_default():
    """No network override → the product default the alerter fires on ($5)."""
    assert spend_alert.daily_spend_alert_threshold_usd({}) == 5.0
    assert (
        spend_alert.daily_spend_alert_threshold_usd({})
        == spend_alert.DEFAULT_DAILY_SPEND_ALERT_USD
    )


def test_alert_threshold_reads_thresholds_override():
    """``thresholds.dailySpendAlertUsd`` is honored — no drift from config."""
    cfg = {"thresholds": {"dailySpendAlertUsd": 8.0}}
    assert spend_alert.daily_spend_alert_threshold_usd(cfg) == 8.0


def test_alert_threshold_alerts_block_wins():
    """``alerts.spendThresholdUSD`` overrides the thresholds value (matches
    the exact precedence ``main()`` applies)."""
    cfg = {
        "alerts": {"spendThresholdUSD": 7.0},
        "thresholds": {"dailySpendAlertUsd": 8.0},
    }
    assert spend_alert.daily_spend_alert_threshold_usd(cfg) == 7.0
