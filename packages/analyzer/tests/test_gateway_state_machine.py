"""Tests for gateway_state_machine.

Pinned behavior (Security_bot-style flap suppression):
  - 1st FAIL  → suppressed (consecutive < confirm_failures)
  - 2nd FAIL  → SURFACED + new_confirmation (consecutive >= confirm_failures)
  - 3rd+ FAIL → SURFACED (continuing outage, state.confirmed_down stays True)
  - PASS after confirmed-down → SURFACED + recovery (sweep_resolve clears)
  - PASS without prior confirmation → SURFACED unchanged (no state churn)
  - WARN status → passes through, no state mutation

State is persisted across calls; load + save are atomic.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import gateway_state_machine as gsm  # noqa: E402


# ── apply_state_machine: core rules ──────────────────────────────────


def test_first_fail_is_suppressed():
    probes = [gsm.GatewayProbe(name="admin_bot:gateway", status="FAIL")]
    state: dict[str, gsm.GatewayState] = {}
    decision = gsm.apply_state_machine(probes, state, confirm_failures=2)
    assert decision.surface == []
    assert decision.suppressed_pending == ["admin_bot:gateway"]
    assert decision.new_confirmations == []
    assert state["admin_bot:gateway"].consecutive_failures == 1
    assert state["admin_bot:gateway"].confirmed_down is False


def test_second_consecutive_fail_confirms_and_surfaces():
    state: dict[str, gsm.GatewayState] = {}
    # 1st fail — suppressed
    gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=2,
    )
    # 2nd fail — confirms
    decision = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=2,
    )
    assert len(decision.surface) == 1
    assert decision.surface[0].name == "admin_bot:gateway"
    assert decision.new_confirmations == ["admin_bot:gateway"]
    assert state["admin_bot:gateway"].confirmed_down is True
    assert state["admin_bot:gateway"].consecutive_failures == 2


def test_third_fail_after_confirmation_keeps_surfacing_no_new_confirmation():
    state: dict[str, gsm.GatewayState] = {}
    for _ in range(2):  # get to confirmed_down
        gsm.apply_state_machine(
            [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=2,
        )
    # 3rd fail — continuing outage
    decision = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=2,
    )
    assert len(decision.surface) == 1
    # NOT a new confirmation; we already confirmed last tick
    assert decision.new_confirmations == []
    assert state["admin_bot:gateway"].consecutive_failures == 3
    assert state["admin_bot:gateway"].confirmed_down is True


def test_pass_after_confirmed_down_emits_recovery():
    state: dict[str, gsm.GatewayState] = {}
    for _ in range(2):  # confirm down
        gsm.apply_state_machine(
            [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=2,
        )
    # Now PASS
    decision = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "PASS")], state, confirm_failures=2,
    )
    assert len(decision.surface) == 1
    assert decision.surface[0].status == "PASS"
    assert decision.recoveries == ["admin_bot:gateway"]
    assert state["admin_bot:gateway"].confirmed_down is False
    assert state["admin_bot:gateway"].consecutive_failures == 0


def test_pass_without_prior_failure_no_recovery_signal():
    state: dict[str, gsm.GatewayState] = {}
    decision = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "PASS")], state, confirm_failures=2,
    )
    assert len(decision.surface) == 1
    assert decision.recoveries == []


def test_warn_passes_through_unchanged():
    state: dict[str, gsm.GatewayState] = {}
    decision = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_ui", "WARN")], state, confirm_failures=2,
    )
    assert len(decision.surface) == 1
    assert decision.surface[0].status == "WARN"
    assert decision.suppressed_pending == []


def test_flap_pattern_only_emits_recovery_after_confirmed_outage():
    """A real flap: FAIL → PASS → FAIL → PASS. None of these should
    produce recovery signals because no outage was ever confirmed."""
    state: dict[str, gsm.GatewayState] = {}
    seq = ["FAIL", "PASS", "FAIL", "PASS"]
    recoveries_total = []
    for status in seq:
        d = gsm.apply_state_machine(
            [gsm.GatewayProbe("admin_bot:gateway", status)], state, confirm_failures=2,
        )
        recoveries_total.extend(d.recoveries)
    assert recoveries_total == [], "flap shouldn't ever fire a recovery signal"


def test_multiple_gateways_tracked_independently():
    state: dict[str, gsm.GatewayState] = {}
    # admin_bot fails twice — confirms; team_bot_b passes
    for _ in range(2):
        gsm.apply_state_machine(
            [
                gsm.GatewayProbe("admin_bot:gateway", "FAIL"),
                gsm.GatewayProbe("team_bot_b:gateway", "PASS"),
            ],
            state, confirm_failures=2,
        )
    assert state["admin_bot:gateway"].confirmed_down is True
    assert state["team_bot_b:gateway"].confirmed_down is False


def test_custom_confirm_threshold():
    state: dict[str, gsm.GatewayState] = {}
    # confirm_failures=4 — need 4 consecutive fails
    for i in range(3):
        d = gsm.apply_state_machine(
            [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=4,
        )
        assert d.surface == [], f"tick {i+1} should still be suppressed"
    d = gsm.apply_state_machine(
        [gsm.GatewayProbe("admin_bot:gateway", "FAIL")], state, confirm_failures=4,
    )
    assert len(d.surface) == 1
    assert d.new_confirmations == ["admin_bot:gateway"]


# ── persistence ──────────────────────────────────────────────────────


def test_load_state_returns_empty_when_missing(tmp_path: Path):
    assert gsm.load_state(tmp_path) == {}


def test_save_then_load_roundtrip(tmp_path: Path):
    state = {
        "admin_bot:gateway": gsm.GatewayState(
            name="admin_bot:gateway",
            consecutive_failures=3, confirmed_down=True,
            last_status="FAIL", last_changed_at=1000000.0,
        ),
    }
    gsm.save_state(tmp_path, state)
    loaded = gsm.load_state(tmp_path)
    assert loaded["admin_bot:gateway"].consecutive_failures == 3
    assert loaded["admin_bot:gateway"].confirmed_down is True
    assert loaded["admin_bot:gateway"].last_status == "FAIL"


def test_corrupt_state_file_loads_as_empty(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "gateway-state.json").write_text("not json {{{")
    assert gsm.load_state(tmp_path) == {}


def test_state_file_schema_includes_version(tmp_path: Path):
    state = {"x:gateway": gsm.GatewayState(name="x:gateway", consecutive_failures=1)}
    gsm.save_state(tmp_path, state)
    data = json.loads((tmp_path / "state" / "gateway-state.json").read_text())
    assert data["schema_version"] == gsm.STATE_SCHEMA_VERSION


# ── filter_gateway_checks (HealthReport wrapper) ─────────────────────


@dataclass
class _FakeCheck:
    status: str
    category: str
    name: str
    detail: str = ""


def test_filter_gateway_checks_suppresses_first_fail(tmp_path: Path):
    checks = [
        _FakeCheck("FAIL", "gateways", "admin_bot:gateway"),
        _FakeCheck("PASS", "gateways", "team_bot_b:gateway"),
        _FakeCheck("FAIL", "launchd_state", "ai.evolve.evolve.audit"),  # other category — unchanged
    ]
    filtered, decision = gsm.filter_gateway_checks(checks, tmp_path)
    # First fail suppressed; PASS passes through; other-category FAIL untouched.
    names = [c.name for c in filtered]
    assert "admin_bot:gateway" not in names
    assert "team_bot_b:gateway" in names
    assert "ai.evolve.evolve.audit" in names
    assert decision.suppressed_pending == ["admin_bot:gateway"]


def test_filter_gateway_checks_surfaces_after_confirmation(tmp_path: Path):
    # Tick 1: FAIL suppressed
    gsm.filter_gateway_checks(
        [_FakeCheck("FAIL", "gateways", "admin_bot:gateway")], tmp_path,
    )
    # Tick 2: FAIL confirmed + surfaced
    filtered, decision = gsm.filter_gateway_checks(
        [_FakeCheck("FAIL", "gateways", "admin_bot:gateway")], tmp_path,
    )
    assert len(filtered) == 1
    assert filtered[0].name == "admin_bot:gateway"
    assert decision.new_confirmations == ["admin_bot:gateway"]


def test_filter_gateway_checks_persists_across_calls(tmp_path: Path):
    """State must survive between invocations — that's the whole point."""
    gsm.filter_gateway_checks(
        [_FakeCheck("FAIL", "gateways", "admin_bot:gateway")], tmp_path,
    )
    state = gsm.load_state(tmp_path)
    assert state["admin_bot:gateway"].consecutive_failures == 1
    gsm.filter_gateway_checks(
        [_FakeCheck("FAIL", "gateways", "admin_bot:gateway")], tmp_path,
    )
    state = gsm.load_state(tmp_path)
    assert state["admin_bot:gateway"].consecutive_failures == 2
    assert state["admin_bot:gateway"].confirmed_down is True
