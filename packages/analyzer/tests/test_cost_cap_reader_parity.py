"""Ratchet: every cost-cap reader must report the SAME effective cap.

The 2026-07-31 defect: ``better-engine-config.json::pod_defaults.budget``
carried the pod ladder (warn $5 / tier_downgrade $15 / hard $20 / L2 $50), and
the readers disagreed about whether it was a fallback.

  * ``spend_caps.get_caps_config`` (feeds ``GET /api/spend-caps``, i.e. the UI)
    read ``pod_defaults.budget`` and reported a $20 daily cap.
  * ``spend_alert._resolve_per_bot_caps`` (the ENFORCEMENT path) read only
    ``bots.<id>.budget`` and returned ``None`` for every rung of every bot
    without an explicit override. Enforcement is gated on
    ``if cap is not None and spend >= cap``, so ``None`` meant no cap, no
    tier-downgrade, no L2 breaker — silently.
  * ``cost_watchdog._resolve_per_bot_cap`` had the same per-bot-only read.
  * the evo ``pod_state.cost_caps`` tool computed its own "effective" values.

Six bots ran with zero enforced cap behind a UI that displayed $20; one spent
$33.78 in a day. The divergence IS the bug, so this file pins the readers
together rather than pinning any one of them to a number. Every reader now
resolves through ``BetterEngineConfig.resolve_budget_ladder``; if a future
change reintroduces a bespoke walk of the config, these tests go red.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import better_engine_config as bec  # noqa: E402
import cost_watchdog  # noqa: E402
import spend_alert  # noqa: E402
import spend_caps  # noqa: E402


# The reference deployment's pod ladder as of the incident.
_POD_LADDER = {
    "per_bot_daily_warn_usd": 5.0,
    "tier_downgrade_usd": 15.0,
    "per_bot_daily_hard_usd": 20.0,
    "l2_breaker_usd": 50.0,
    "per_bot_session_cost_cap_usd": 10.0,
}

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _write_be(shared_dir: Path, *, pod: dict, bots: dict | None = None) -> None:
    payload = {
        "schema_version": 1,
        "pod_defaults": {"budget": dict(pod)},
        "bots": bots or {},
    }
    (shared_dir / "better-engine-config.json").write_text(json.dumps(payload))


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


# ─── The headline ratchet: UI reader == enforcement reader ─────────────────


def test_ui_and_enforcement_agree_for_bot_without_override(shared):
    """The exact incident shape: pod ladder set, bot has no override.

    Pre-fix the UI said $20 and the enforcer said ``None``.
    """
    _write_be(shared, pod=_POD_LADDER)

    ui_cap = spend_caps.get_caps_config({}, shared)["dailySpendCapUsd"]
    enforced_cap = spend_alert._resolve_per_bot_caps("team_bot_a", shared)["l1_breaker"]

    assert ui_cap == enforced_cap == 20.0


def test_enforcement_resolves_every_rung_from_pod_defaults(shared):
    """Not just the hard cap — tier_downgrade and L2 were ``None`` too."""
    _write_be(shared, pod=_POD_LADDER)

    ladder = spend_alert._resolve_per_bot_caps("team_bot_a", shared)
    assert ladder == {
        "daily_warn": 5.0,
        "weekly_warn": None,          # genuinely unset at both scopes
        "tier_downgrade": 15.0,
        "l1_breaker": 20.0,
        "l2_breaker": 50.0,
        "per_session": 10.0,
    }


def test_enforcement_gate_actually_fires_at_the_pod_cap(shared):
    """Pin the consequence, not just the value.

    ``spend_alert`` gates every rung on ``cap is not None and spend >= cap``.
    At $33.78 spend against the pod's $20 hard cap, all three remediation
    rungs must evaluate true — pre-fix every one of them was skipped.
    """
    _write_be(shared, pod=_POD_LADDER)
    ladder = spend_alert._resolve_per_bot_caps("team_bot_a", shared)
    spend = 33.78

    fired = {
        rung: (ladder[rung] is not None and spend >= ladder[rung])
        for rung in ("tier_downgrade", "l1_breaker")
    }
    assert fired == {"tier_downgrade": True, "l1_breaker": True}
    # $33.78 is under the $50 L2 rung — it must NOT fire.
    assert not (ladder["l2_breaker"] is not None and spend >= ladder["l2_breaker"])


def test_explicit_per_bot_override_wins_over_pod_default(shared):
    """Some bots sit at a deliberately tighter $5. The fallback must fill only
    the rungs a bot leaves unset — never loosen an explicit one to the pod's
    $20."""
    _write_be(
        shared,
        pod=_POD_LADDER,
        bots={"security_bot": {"budget": {"per_bot_daily_hard_usd": 5.0}}},
    )
    assert spend_alert._resolve_per_bot_caps("security_bot", shared)["l1_breaker"] == 5.0
    assert spend_alert._resolve_per_bot_caps("team_bot_a", shared)["l1_breaker"] == 20.0


def test_no_pod_default_still_means_no_cap(shared):
    """The fallback is to the stored pod default, NOT past it to the compiled $5.

    The compiled default arms the Budget Hawk guardian veto, not a breaker.
    Arming L1 trips fleet-wide off a value the operator never chose was
    rejected in the graduated-cap work (see
    test_new_bot_graduated_cap.py). Both readers must report that honestly
    rather than one of them inventing a cap.
    """
    _write_be(shared, pod={"per_bot_daily_warn_usd": 5.0})

    ui_cap = spend_caps.get_caps_config({}, shared)["dailySpendCapUsd"]
    enforced_cap = spend_alert._resolve_per_bot_caps("team_bot_a", shared)["l1_breaker"]

    assert ui_cap is None
    assert enforced_cap is None


def test_guardian_veto_and_breaker_diverge_only_when_nothing_configured(shared):
    """Pin the ONE designed asymmetry so nobody "fixes" it into a surprise.

    With no cap at any scope, ``budget_hard_cap_usd`` returns the compiled $5
    (Budget Hawk vetoes proposals above it) while the enforced ladder returns
    None (no breaker trips). Both are intentional; the UI reader sides with
    the enforcer, because a displayed cap must be one that fires.
    """
    _write_be(shared, pod={}, bots={"bare": {}})
    cfg = bec.load(shared)

    assert cfg.budget_hard_cap_usd("bare") == 5.00
    assert spend_alert._resolve_per_bot_caps("bare", shared)["l1_breaker"] is None
    assert spend_caps.get_caps_config({}, shared)["dailySpendCapUsd"] is None


def test_zero_pod_default_reads_as_uncapped_in_both_readers(shared):
    _write_be(shared, pod={**_POD_LADDER, "per_bot_daily_hard_usd": 0})

    assert spend_caps.get_caps_config({}, shared)["dailySpendCapUsd"] is None
    assert spend_alert._resolve_per_bot_caps("team_bot_a", shared)["l1_breaker"] is None


# ─── The other two readers ─────────────────────────────────────────────────


def test_cost_watchdog_agrees_with_enforcement(shared):
    """cost_watchdog sizes its heartbeat-cost detectors against the cap; it
    saw ``None`` for every pod-default bot post-Phase-8."""
    _write_be(shared, pod=_POD_LADDER)

    enforced = spend_alert._resolve_per_bot_caps("team_bot_a", shared)["l1_breaker"]
    assert cost_watchdog._resolve_per_bot_cap("team_bot_a", {}, shared) == enforced == 20.0


def test_evo_cost_caps_tool_agrees_with_enforcement(shared):
    """``pod_state.cost_caps``'s ``effective`` block is what evo tells the
    operator is in force."""
    _write_be(shared, pod=_POD_LADDER)
    cfg = bec.load(shared)

    effective = {
        rung + "_usd": value
        for rung, value in cfg.resolve_budget_ladder("team_bot_a").items()
    }
    enforced = spend_alert._resolve_per_bot_caps("team_bot_a", shared)

    assert effective["l1_breaker_usd"] == enforced["l1_breaker"] == 20.0
    assert effective["tier_downgrade_usd"] == enforced["tier_downgrade"] == 15.0


# ─── Precedence layers the parity must survive ─────────────────────────────


def test_graduated_new_bot_default_outranks_pod_default(shared):
    """A bot inside its activation window keeps the compiled $10 backstop
    even when the pod default is looser."""
    created = "2026-07-29T00:00:00+00:00"  # 2 days before _NOW
    _write_be(shared, pod=_POD_LADDER, bots={"fresh": {"created_at": created}})

    ladder = spend_alert._resolve_per_bot_caps("fresh", shared, now=_NOW)
    assert ladder["l1_breaker"] == bec.NEW_BOT_DAILY_HARD_USD
    # …and the other rungs still inherit the pod ladder.
    assert ladder["tier_downgrade"] == 15.0


def test_matured_bot_falls_through_to_pod_default(shared):
    created = "2026-01-01T00:00:00+00:00"  # long graduated
    _write_be(shared, pod=_POD_LADDER, bots={"veteran": {"created_at": created}})

    ladder = spend_alert._resolve_per_bot_caps("veteran", shared, now=_NOW)
    assert ladder["l1_breaker"] == 20.0


def test_monthly_budget_derivation_reaches_enforcement(shared):
    """A bot configured via Bot setup's single ``per_bot_monthly_cap_usd``
    knob must be enforced on the derived daily cap.

    Budget Hawk (``budget_hard_cap_usd``) has always derived it; the enforcer
    never did, so a monthly-budget bot was uncapped in exactly the same silent
    way a pod-default bot was.
    """
    _write_be(
        shared,
        pod=_POD_LADDER,
        bots={"team_bot_c": {"budget": {"per_bot_monthly_cap_usd": 60.0}}},
    )
    cfg = bec.load(shared)
    derived = cfg.budget_hard_cap_usd("team_bot_c")

    enforced = spend_alert._resolve_per_bot_caps("team_bot_c", shared)["l1_breaker"]
    assert enforced == pytest.approx(derived) == pytest.approx(60.0 / 30 * 2.5)


def test_budget_hawk_and_enforcement_agree_on_hard_cap(shared):
    """``budget_hard_cap_usd`` (the guardian veto) and the enforced L1 rung are
    two independent implementations of the same precedence chain. Pin them
    equal across the cases where they can drift.

    They agree everywhere a cap is configured at some scope. The one designed
    exception is pinned separately below.
    """
    _write_be(
        shared,
        pod=_POD_LADDER,
        bots={
            "plain": {},
            "override": {"budget": {"per_bot_daily_hard_usd": 7.0}},
            "monthly": {"budget": {"per_bot_monthly_cap_usd": 60.0}},
            "fresh": {"created_at": "2026-07-29T00:00:00+00:00"},
        },
    )
    cfg = bec.load(shared)
    for bot_id in ("plain", "override", "monthly", "fresh"):
        hawk = cfg.budget_hard_cap_usd(bot_id, now=_NOW)
        enforced = spend_alert._resolve_per_bot_caps(bot_id, shared, now=_NOW)["l1_breaker"]
        assert enforced == pytest.approx(hawk), bot_id


# ─── Structural pins ───────────────────────────────────────────────────────


def test_spend_alert_rung_list_matches_be_config(shared):
    """``spend_alert._LADDER_RUNGS`` duplicates the BE-config rung names so the
    all-None fallback works without BE config importable. Keep them equal."""
    assert spend_alert._LADDER_RUNGS == tuple(bec.BUDGET_LADDER_FIELDS)


def test_spend_alert_has_no_second_cap_reader():
    """The dead pod-wide branch read ``thresholds.dailySpendCapUsd`` straight
    off network.json — a second reader that disagreed with the ladder and was
    unreachable post-migration. Enforcement must not grow another one."""
    tree = ast.parse((_ANALYZER_DIR / "spend_alert.py").read_text())
    # Only an exact-match string constant is a config-key read; a docstring
    # mentioning the legacy key is a whole-paragraph constant and won't match.
    offending = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "dailySpendCapUsd"
    ]
    assert offending == [], (
        f"spend_alert reads dailySpendCapUsd at line(s) "
        f"{[n.lineno for n in offending]}"
    )
