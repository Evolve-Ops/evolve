"""Graduated new-bot daily hard cap (decision A of the new-bot cost-defaults
design-sync; META:user-value).

A freshly-created bot gets a product-default daily hard cap that ships in
code (no per-pod proposal, nothing materialized into config):

    explicit per-bot override  >  graduated new-bot default  >  pod default

    - first ``NEW_BOT_GRADUATION_DAYS`` days → ``NEW_BOT_DAILY_HARD_USD`` ($10)
    - thereafter                           → the pod default ($5 compiled)

Why: the ``ledger`` bot spent $30.26 in its first two days with no per-bot
cap — the backstop was absent on new bots, so the warn alert fired but
nothing stopped the spend. Finding:
docs/finding-new-bot-activation-cost-2026-06-12.md.

This file pins:
  1. the resolution (``BetterEngineConfig.budget_hard_cap_usd``) — the proof
     artifact: age 3d → $10, age 10d → $5, explicit override still wins;
  2. the L1-breaker wiring (``spend_alert._resolve_per_bot_caps``) — a new bot
     gets a real per-bot L1 cap (the rung that actually pauses spend), while
     mature uncapped bots are unchanged.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_alert  # noqa: E402
from better_engine_config import (  # noqa: E402
    NEW_BOT_DAILY_HARD_USD,
    NEW_BOT_GRADUATION_DAYS,
    BetterEngineConfig,
)

_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _cfg_with_age(bot_id: str, age_days: float) -> BetterEngineConfig:
    """A default (compiled) config with ``bot_id`` created ``age_days`` ago."""
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at(bot_id, _NOW - timedelta(days=age_days))
    return cfg


# ─── Proof artifact: budget_hard_cap_usd graduated resolution ───────────────


def test_new_bot_age_3d_resolves_to_ten_dollars():
    """A bot 3 days old (inside the activation window) → $10/day."""
    cfg = _cfg_with_age("ledger", age_days=3)
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 10.00


def test_same_bot_age_10d_resolves_to_five_dollars():
    """The same bot 10 days old (past the window) → $5/day (pod default)."""
    cfg = _cfg_with_age("ledger", age_days=10)
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 5.00


def test_explicit_per_bot_override_still_wins_inside_window():
    """An explicit per-bot cap beats the graduated default even on day 3."""
    cfg = _cfg_with_age("ledger", age_days=3)
    cfg.set_per_bot_daily_hard_usd("ledger", 3.00)
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 3.00


def test_explicit_override_wins_above_graduated_too():
    """A higher explicit cap (e.g. a high-spend primary) also wins."""
    cfg = _cfg_with_age("evolve", age_days=2)
    cfg.set_per_bot_daily_hard_usd("evolve", 25.00)
    assert cfg.budget_hard_cap_usd("evolve", now=_NOW) == 25.00


def test_explicit_zero_means_uncapped_not_graduated():
    """A stored 0 is the operator's explicit 'uncapped' — graduation must not
    silently re-cap it (presence-based override semantics)."""
    cfg = _cfg_with_age("ledger", age_days=3)
    cfg.set_per_bot_daily_hard_usd("ledger", 0)
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 0.0


def test_monthly_cap_still_takes_precedence_over_graduated():
    """An explicit per-bot monthly cap derives hard/warn and wins over the
    graduated default (unchanged precedence)."""
    cfg = _cfg_with_age("ledger", age_days=3)
    cfg.set_per_bot_monthly_cap_usd("ledger", 90.0)  # 90/30 * 2.5 = 7.5
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == pytest.approx(7.5)


# ─── Window boundaries + missing/invalid stamps ─────────────────────────────


def test_window_boundary_exactly_seven_days_is_mature():
    """At exactly NEW_BOT_GRADUATION_DAYS the bot has graduated → pod default."""
    cfg = _cfg_with_age("ledger", age_days=NEW_BOT_GRADUATION_DAYS)
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 5.00


def test_just_inside_window_is_graduated():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at(
        "ledger", _NOW - timedelta(days=NEW_BOT_GRADUATION_DAYS) + timedelta(hours=1)
    )
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == NEW_BOT_DAILY_HARD_USD


def test_no_created_at_stamp_uses_pod_default():
    """Existing / pre-feature bots carry no stamp → no graduation → pod default.
    A fresh-install pod gets all of this with zero proposals / zero config."""
    cfg = BetterEngineConfig.default()
    assert cfg.budget_hard_cap_usd("legacy_bot", now=_NOW) == 5.00


def test_future_stamp_clock_skew_does_not_graduate():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW + timedelta(days=2))
    assert cfg.budget_hard_cap_usd("ledger", now=_NOW) == 5.00


def test_graduated_helper_returns_none_outside_window():
    cfg = _cfg_with_age("ledger", age_days=30)
    assert cfg.new_bot_graduated_hard_cap_usd("ledger", now=_NOW) is None


def test_graduated_helper_returns_constant_inside_window():
    cfg = _cfg_with_age("ledger", age_days=1)
    assert (
        cfg.new_bot_graduated_hard_cap_usd("ledger", now=_NOW)
        == NEW_BOT_DAILY_HARD_USD
    )


# ─── created_at stamp storage round-trip ────────────────────────────────────


def test_created_at_round_trips_through_serialization():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW)
    reloaded = BetterEngineConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert reloaded.bot_created_at("ledger") == _NOW


def test_set_created_at_none_clears_stamp():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW)
    cfg.set_bot_created_at("ledger", None)
    assert cfg.bot_created_at("ledger") is None


def test_bot_created_at_parses_naive_as_utc():
    cfg = BetterEngineConfig.from_dict(
        {"schema_version": 1, "pod_defaults": {}, "bots": {"ledger": {"created_at": "2026-06-09T12:00:00"}}}
    )
    got = cfg.bot_created_at("ledger")
    assert got == datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_bot_created_at_parses_z_suffix():
    cfg = BetterEngineConfig.from_dict(
        {"schema_version": 1, "pod_defaults": {}, "bots": {"ledger": {"created_at": "2026-06-09T12:00:00Z"}}}
    )
    assert cfg.bot_created_at("ledger") == datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_bot_created_at_unparseable_is_none():
    cfg = BetterEngineConfig.from_dict(
        {"schema_version": 1, "pod_defaults": {}, "bots": {"ledger": {"created_at": "not-a-date"}}}
    )
    assert cfg.bot_created_at("ledger") is None


def test_naive_now_is_treated_as_utc():
    """A caller passing a naive ``now`` must not raise (tz-aware - naive)."""
    cfg = _cfg_with_age("ledger", age_days=3)
    naive_now = _NOW.replace(tzinfo=None)
    assert cfg.budget_hard_cap_usd("ledger", now=naive_now) == 10.00


# ─── L1-breaker enforcement wiring (spend_alert) ────────────────────────────


def _write_be_with_age(shared_dir: Path, bot_id: str, age_days: float | None,
                       **budget) -> None:
    bot: dict = {"budget": budget}
    if age_days is not None:
        bot["created_at"] = (_NOW - timedelta(days=age_days)).isoformat()
    payload = {"schema_version": 1, "pod_defaults": {}, "bots": {bot_id: bot}}
    (shared_dir / "better-engine-config.json").write_text(json.dumps(payload))


def test_l1_breaker_armed_for_new_bot_without_explicit_cap(tmp_path):
    """The fix: a new bot with no explicit cap still gets a real L1 cap, so the
    rung that pauses heartbeat + background spend actually fires."""
    _write_be_with_age(tmp_path, "ledger", age_days=3)
    caps = spend_alert._resolve_per_bot_caps("ledger", tmp_path, now=_NOW)
    assert caps["l1_breaker"] == NEW_BOT_DAILY_HARD_USD


def test_l1_breaker_none_for_mature_bot_without_explicit_cap(tmp_path):
    """Mature uncapped bots are unchanged — no new fleet-wide L1 trips."""
    _write_be_with_age(tmp_path, "veteran", age_days=None)
    caps = spend_alert._resolve_per_bot_caps("veteran", tmp_path, now=_NOW)
    assert caps["l1_breaker"] is None


def test_l1_breaker_explicit_cap_wins_for_new_bot(tmp_path):
    _write_be_with_age(tmp_path, "ledger", age_days=3, per_bot_daily_hard_usd=4.0)
    caps = spend_alert._resolve_per_bot_caps("ledger", tmp_path, now=_NOW)
    assert caps["l1_breaker"] == 4.0


def test_l1_breaker_drops_to_none_after_window(tmp_path):
    """Past the activation window an uncapped bot returns to the mature posture
    (None) — the pod default governs it via the guardian veto, not a new L1."""
    _write_be_with_age(tmp_path, "ledger", age_days=10)
    caps = spend_alert._resolve_per_bot_caps("ledger", tmp_path, now=_NOW)
    assert caps["l1_breaker"] is None


def test_resolve_per_bot_cap_singular_sees_graduated(tmp_path):
    _write_be_with_age(tmp_path, "ledger", age_days=3)
    cap = spend_alert._resolve_per_bot_cap("ledger", {}, tmp_path, now=_NOW)
    assert cap == NEW_BOT_DAILY_HARD_USD
