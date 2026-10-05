"""Provisioning budget gate (decision B of the new-bot cost-defaults
design-sync; META:user-value).

A new bot's one-time standup spend (forge app-builds + first audits) is
bounded by a cumulative ceiling that the per-day cap can't express. The
``ledger`` bot spent $30.26 in two days — the $10 daily cap tripped on both
days but couldn't govern the multi-day standup, and operator-confirmed
installs are exempt from the daily cap anyway. This module is the missing
cumulative backstop.

This file is the proof artifact for the GATE (the forge/audit call-site proof
lives in packages/admin/tests/test_forge_provisioning_ceiling.py). It pins:

  1. the $12 product default ships in code + resolves with a per-bot override
     that can't be disabled with a stored 0 (safety direction);
  2. the activation-window gating (ceiling only bites while the bot is new);
  3. the pure refusal logic (``decide``): ceiling, daily breaker, under-budget,
     and the fail-open "spend unknown" case;
  4. the end-to-end ``evaluate`` actually REFUSES against a real BE config +
     real cumulative spend (failure mode i — "ceiling never enforces");
  5. the refusal Signal payload is well-formed + names what remains (failure
     mode ii — "bricks half-provisioned with no signal").

Finding: internal/finding-new-bot-activation-cost-2026-06-12.md.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import provisioning_budget as pb  # noqa: E402
from better_engine_config import (  # noqa: E402
    NEW_BOT_GRADUATION_DAYS,
    NEW_BOT_PROVISIONING_CEILING_USD,
    BetterEngineConfig,
    save as save_be,
)

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


# ─── Config: the $12 product default + per-bot override semantics ────────────


def test_product_default_is_twelve_dollars():
    assert NEW_BOT_PROVISIONING_CEILING_USD == 12.0
    cfg = BetterEngineConfig.default()
    assert cfg.provisioning_ceiling_usd("any-bot") == 12.0


def test_explicit_positive_override_wins():
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_provisioning_ceiling_usd("ledger", 25.0)
    assert cfg.provisioning_ceiling_usd("ledger") == 25.0


@pytest.mark.parametrize("bad", [0.0, -5.0])
def test_zero_or_negative_override_falls_back_to_default(bad):
    """A spend backstop must not be disable-able with a stored 0 — the inverse
    of the per_bot_daily_hard_usd '0 = uncapped' convention, on purpose."""
    cfg = BetterEngineConfig.default()
    cfg.set_per_bot_provisioning_ceiling_usd("ledger", bad)
    assert cfg.provisioning_ceiling_usd("ledger") == 12.0


# ─── Config: activation-window gating ────────────────────────────────────────


def test_in_window_for_fresh_bot():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW - timedelta(days=1))
    assert cfg.in_provisioning_window("ledger", now=_NOW) is True


def test_out_of_window_after_graduation():
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW - timedelta(days=NEW_BOT_GRADUATION_DAYS + 1))
    assert cfg.in_provisioning_window("ledger", now=_NOW) is False


def test_no_created_stamp_is_never_in_window():
    cfg = BetterEngineConfig.default()
    assert cfg.in_provisioning_window("mature-bot", now=_NOW) is False


# ─── Pure decision logic ─────────────────────────────────────────────────────


def test_decide_refuses_at_ceiling():
    allowed, check, reason = pb.decide(
        spent_usd=12.0, ceiling_usd=12.0, in_window=True, breaker_tripped=False,
    )
    assert allowed is False
    assert check == "creation_ceiling"
    assert "ceiling" in reason


def test_decide_refuses_over_ceiling():
    allowed, check, _ = pb.decide(
        spent_usd=30.26, ceiling_usd=12.0, in_window=True, breaker_tripped=False,
    )
    assert allowed is False and check == "creation_ceiling"


def test_decide_allows_under_ceiling():
    allowed, check, _ = pb.decide(
        spent_usd=5.0, ceiling_usd=12.0, in_window=True, breaker_tripped=False,
    )
    assert allowed is True and check == ""


def test_decide_ceiling_inert_out_of_window():
    """Over-ceiling spend doesn't gate a graduated bot — steady-state work is
    governed by the daily cap, not the one-time ceiling."""
    allowed, check, _ = pb.decide(
        spent_usd=99.0, ceiling_usd=12.0, in_window=False, breaker_tripped=False,
    )
    assert allowed is True and check == ""


def test_decide_breaker_refuses_regardless_of_window():
    """The daily cap governs provisioning: a tripped cost breaker pauses
    provisioning on any bot (in window or not)."""
    for in_window in (True, False):
        allowed, check, reason = pb.decide(
            spent_usd=1.0, ceiling_usd=12.0,
            in_window=in_window, breaker_tripped=True,
        )
        assert allowed is False
        assert check == "daily_breaker"
        assert "breaker" in reason


def test_decide_fail_open_when_spend_unknown():
    """A None spend (ledger unreadable) must NOT refuse on the ceiling — the
    breaker check still runs, but an unknown ledger never blocks a dispatch."""
    allowed, check, _ = pb.decide(
        spent_usd=None, ceiling_usd=12.0, in_window=True, breaker_tripped=False,
    )
    assert allowed is True and check == ""


def test_decide_ceiling_takes_precedence_over_breaker_label():
    """When both fire, the ceiling (the headline cumulative bound) is named."""
    allowed, check, _ = pb.decide(
        spent_usd=20.0, ceiling_usd=12.0, in_window=True, breaker_tripped=True,
    )
    assert allowed is False and check == "creation_ceiling"


# ─── window_spend_usd: sums the standup ledger ───────────────────────────────


def _patch_turns(monkeypatch, turns):
    import usage_analytics
    monkeypatch.setattr(usage_analytics, "load_turns", lambda *a, **k: list(turns))


def test_window_spend_sums_turns_since_created(monkeypatch):
    created = _NOW - timedelta(days=1)
    turns = [
        {"ts": (created + timedelta(hours=1)).isoformat(), "cost": 4.0},
        {"ts": (created + timedelta(hours=2)).isoformat(), "cost": 6.5},
        # Before created_at — excluded.
        {"ts": (created - timedelta(hours=1)).isoformat(), "cost": 99.0},
    ]
    _patch_turns(monkeypatch, turns)
    spent = pb.window_spend_usd(
        "ledger", created_at=created, now=_NOW,
        window_days=NEW_BOT_GRADUATION_DAYS,
    )
    assert spent == pytest.approx(10.5)


def test_window_spend_none_on_read_failure(monkeypatch):
    import usage_analytics

    def _boom(*a, **k):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(usage_analytics, "load_turns", _boom)
    spent = pb.window_spend_usd(
        "ledger", created_at=_NOW - timedelta(days=1), now=_NOW,
        window_days=NEW_BOT_GRADUATION_DAYS,
    )
    assert spent is None


# ─── evaluate(): end-to-end refusal against a real config + spend ────────────


def _write_be(tmp_path, *, age_days=1, ceiling=None):
    cfg = BetterEngineConfig.default()
    cfg.set_bot_created_at("ledger", _NOW - timedelta(days=age_days))
    if ceiling is not None:
        cfg.set_per_bot_provisioning_ceiling_usd("ledger", ceiling)
    save_be(cfg, tmp_path)


def test_evaluate_refuses_when_standup_spend_reaches_ceiling(monkeypatch, tmp_path):
    """The load-bearing proof of failure mode (i): a bot that has already burned
    its ceiling is REFUSED, not merely alerted."""
    _write_be(tmp_path, age_days=1)
    created = _NOW - timedelta(days=1)
    _patch_turns(monkeypatch, [
        {"ts": (created + timedelta(hours=1)).isoformat(), "cost": 13.0},
    ])
    monkeypatch.setattr(pb, "cost_breaker_tripped", lambda *a, **k: False)

    decision = pb.evaluate(
        "ledger", tmp_path, kind="install build (journal)",
        job_id="j-1", remaining_label="3 app(s) not yet provisioned", now=_NOW,
    )
    assert decision.allowed is False
    assert decision.failed_check == "creation_ceiling"
    assert decision.spent_usd == pytest.approx(13.0)
    assert decision.ceiling_usd == 12.0
    assert decision.signal_payload is not None


def test_evaluate_allows_when_under_ceiling(monkeypatch, tmp_path):
    _write_be(tmp_path, age_days=1)
    created = _NOW - timedelta(days=1)
    _patch_turns(monkeypatch, [
        {"ts": (created + timedelta(hours=1)).isoformat(), "cost": 4.0},
    ])
    monkeypatch.setattr(pb, "cost_breaker_tripped", lambda *a, **k: False)

    decision = pb.evaluate("ledger", tmp_path, kind="install build", now=_NOW)
    assert decision.allowed is True
    assert decision.signal_payload is None
    assert decision.remaining_usd == pytest.approx(8.0)


def test_evaluate_breaker_pauses_provisioning(monkeypatch, tmp_path):
    """Part 1 proof: a tripped daily cost breaker pauses provisioning even when
    cumulative spend is well under the ceiling."""
    _write_be(tmp_path, age_days=1)
    created = _NOW - timedelta(days=1)
    _patch_turns(monkeypatch, [
        {"ts": (created + timedelta(hours=1)).isoformat(), "cost": 1.0},
    ])
    monkeypatch.setattr(pb, "cost_breaker_tripped", lambda *a, **k: True)

    decision = pb.evaluate("ledger", tmp_path, kind="install build", now=_NOW)
    assert decision.allowed is False
    assert decision.failed_check == "daily_breaker"


def test_evaluate_inert_for_graduated_bot(monkeypatch, tmp_path):
    """A graduated bot, over-spent on the window but with no breaker, is allowed
    — the ceiling only governs the activation window."""
    _write_be(tmp_path, age_days=NEW_BOT_GRADUATION_DAYS + 2)
    monkeypatch.setattr(pb, "cost_breaker_tripped", lambda *a, **k: False)
    # load_turns shouldn't even be consulted (out of window); make it loud if it is.
    import usage_analytics
    monkeypatch.setattr(
        usage_analytics, "load_turns",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not read")),
    )
    decision = pb.evaluate("ledger", tmp_path, kind="install build", now=_NOW)
    assert decision.allowed is True


def test_evaluate_fail_open_on_missing_config(monkeypatch, tmp_path):
    """No BE config on disk → default config → no created_at → not in window →
    allowed. A backstop that can't read its inputs never blocks provisioning."""
    monkeypatch.setattr(pb, "cost_breaker_tripped", lambda *a, **k: False)
    decision = pb.evaluate("ledger", tmp_path, kind="install build", now=_NOW)
    assert decision.allowed is True


# ─── Signal payload: observable cap (failure mode ii) ────────────────────────


def test_capped_signal_is_well_formed_and_names_remaining():
    payload = pb.build_capped_signal(
        bot_id="ledger", kind="install build (journal)", job_id="j-9",
        failed_check="creation_ceiling",
        reason="cumulative standup spend $13.00 has reached the $12.00 ceiling",
        ceiling_usd=12.0, spent_usd=13.0,
        remaining_label="3 app(s) not yet provisioned (incl. journal)",
    )
    assert payload["type"] == "provisioning_budget_exceeded"
    assert payload["producer"] == "provisioning_budget"
    assert payload["bot_id"] == "ledger"
    assert payload["scope"] == "bot"
    # Names what remains — the half-provisioned-safety proof.
    assert "3 app(s) not yet provisioned" in payload["body"]
    assert payload["details"]["failed_check"] == "creation_ceiling"
    assert payload["details"]["spent_usd"] == 13.0
    assert payload["details"]["ceiling_usd"] == 12.0


def test_ceiling_and_breaker_signatures_differ():
    """Distinct refusal reasons → distinct Signals (different response paths);
    repeated refusals under one reason dedup into one firing."""
    ceil = pb.build_capped_signal(
        bot_id="b", kind="k", job_id="", failed_check="creation_ceiling",
        reason="r", ceiling_usd=12.0, spent_usd=13.0,
    )
    brk = pb.build_capped_signal(
        bot_id="b", kind="k", job_id="", failed_check="daily_breaker",
        reason="r", ceiling_usd=12.0, spent_usd=1.0,
    )
    assert ceil["signature"] != brk["signature"]
