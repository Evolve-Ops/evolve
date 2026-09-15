"""tests/test_evo_cost_caps_phase9.py — Phase 9 evo tool surface for the
graduated cost-cap ladder.

Spec: internal/spec-cost-caps-2026-06-05.md.

Covers:
- pod_state.cost_caps        — read all 6 ladder fields + pod-default
  inheritance + effective values
- pod_state.cost_remediation_status — read tier_downgrade flag + L1/L2
  breaker state
- action.cost.set_cap        — generic field setter with ladder validation
- action.cost.reset_remediation — manual reset for tier_downgrade / L1 / L2
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def pod_env(tmp_path: Path):
    """Stand up a minimal pod env: shared_dir + network.json + BE config."""
    shared = tmp_path / "shared"
    shared.mkdir()
    net_path = tmp_path / "network.json"
    net = {
        "sharedDir": str(shared),
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"role": "member"}},
    }
    net_path.write_text(json.dumps(net))
    return shared, net_path


def _write_be(shared: Path, **bot_budget):
    payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"team_bot_a": {"budget": bot_budget}},
    }
    (shared / "better-engine-config.json").write_text(json.dumps(payload))


# ─── pod_state.cost_caps ──────────────────────────────────────────────────


def test_cost_caps_returns_six_ladder_fields(pod_env):
    shared, net_path = pod_env
    _write_be(
        shared,
        per_bot_daily_warn_usd=5.0,
        tier_downgrade_usd=8.0,
        per_bot_daily_hard_usd=10.0,
        l2_breaker_usd=20.0,
        weekly_warn_usd=30.0,
        per_bot_session_cost_cap_usd=2.0,
    )
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_caps_handler
    result = _cost_caps_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    pb = result["per_bot"]
    assert pb["daily_warn_usd"] == 5.0
    assert pb["tier_downgrade_usd"] == 8.0
    assert pb["l1_breaker_usd"] == 10.0
    assert pb["l2_breaker_usd"] == 20.0
    assert pb["weekly_warn_usd"] == 30.0
    assert pb["per_session_cap_usd"] == 2.0


def test_cost_caps_effective_is_null_when_nothing_is_configured(pod_env):
    """``effective`` reports what will actually TRIP, and nothing trips here.

    This used to report the compiled $5 default (via ``budget_hard_cap_usd``),
    which is the threshold the Budget Hawk *guardian veto* uses — not a
    threshold any breaker fires on. Mature bots with no explicit and no pod
    default deliberately get no L1 breaker (see
    ``test_new_bot_graduated_cap.py::
    test_l1_breaker_none_for_mature_bot_without_explicit_cap`` — arming L1
    trips fleet-wide off a default the operator never chose was rejected).

    Telling the operator "$5 is in force" when no enforcer will act on it is
    the exact shape of the 2026-07-31 defect. ``null`` is the honest answer.
    """
    shared, net_path = pod_env
    _write_be(shared)  # empty per-bot budget, empty pod_defaults
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_caps_handler
    result = _cost_caps_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    assert result["effective"]["l1_breaker_usd"] is None


def test_cost_caps_effective_inherits_pod_default(pod_env):
    """The fallback that was missing: a bot with no per-bot value reports (and
    is enforced at) the pod-default rung."""
    shared, net_path = pod_env
    payload = {
        "schema_version": 1,
        "pod_defaults": {"budget": {
            "per_bot_daily_hard_usd": 20.0, "tier_downgrade_usd": 15.0,
        }},
        "bots": {"team_bot_a": {"budget": {}}},
    }
    (shared / "better-engine-config.json").write_text(json.dumps(payload))
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_caps_handler
    result = _cost_caps_handler(net_path, "team_bot_a")
    assert result["effective"]["l1_breaker_usd"] == 20.0
    assert result["effective"]["tier_downgrade_usd"] == 15.0
    # …and the per_bot block still reports "no explicit override", so the UI
    # can keep showing the inherited-vs-pinned distinction.
    assert result["per_bot"]["l1_breaker_usd"] is None


def test_cost_caps_rejects_unknown_bot(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_caps_handler
    result = _cost_caps_handler(net_path, "no_such_bot")
    assert result["ok"] is False
    assert "unknown bot" in result["error"]


def test_cost_caps_requires_bot_id(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_caps_handler
    result = _cost_caps_handler(net_path, None)
    assert result["ok"] is False
    assert "bot_id" in result["error"]


# ─── pod_state.cost_remediation_status ────────────────────────────────────


def test_remediation_status_all_inactive_by_default(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_remediation_status_handler
    result = _cost_remediation_status_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    assert result["tier_downgrade"]["active"] is False
    assert result["l1_breaker"]["tripped"] is False
    assert result["l2_breaker"]["tripped"] is False


def test_remediation_status_reads_tier_downgrade_flag(pod_env):
    shared, net_path = pod_env
    from datetime import date
    flag_dir = shared / "cost_remediations" / "team_bot_a"
    flag_dir.mkdir(parents=True)
    (flag_dir / "tier_downgrade.flag").write_text(str(date.today()))
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_remediation_status_handler
    result = _cost_remediation_status_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    assert result["tier_downgrade"]["active"] is True


def test_remediation_status_reads_l1_breaker(pod_env):
    shared, net_path = pod_env
    breaker_dir = shared / "breakers" / "team_bot_a"
    breaker_dir.mkdir(parents=True)
    (breaker_dir / "cost.json").write_text(json.dumps({
        "bot_id": "team_bot_a",
        "type": "cost",
        "tripped_at": "2026-06-06T10:00:00Z",
        "expires_at": "2026-06-07T10:00:00Z",
        "reason": "test trip",
        "trip_id": "abc12345",
        "initiated_by": "test",
    }))
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_remediation_status_handler
    result = _cost_remediation_status_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    assert result["l1_breaker"]["tripped"] is True
    assert result["l1_breaker"]["reason"] == "test trip"


def test_remediation_status_reads_l2_breaker(pod_env):
    shared, net_path = pod_env
    breaker_dir = shared / "breakers" / "team_bot_a"
    breaker_dir.mkdir(parents=True)
    (breaker_dir / "cost_l2.json").write_text(json.dumps({
        "bot_id": "team_bot_a",
        "type": "cost_l2",
        "tripped_at": "2026-06-06T10:00:00Z",
        "reason": "L2 trip",
        "trip_id": "def67890",
        "initiated_by": "auto:spend_alert",
    }))
    from evolve_admin.evo.tools.pod_state_cost_caps import _cost_remediation_status_handler
    result = _cost_remediation_status_handler(net_path, "team_bot_a")
    assert result["ok"] is True
    assert result["l2_breaker"]["tripped"] is True


# ─── action.cost.set_cap ──────────────────────────────────────────────────


def test_set_cap_writes_each_ladder_field(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_handler
    for field, value in [
        ("daily_warn_usd", 3.0),
        ("tier_downgrade_usd", 6.0),
        ("l1_breaker_usd", 10.0),
        ("l2_breaker_usd", 25.0),
        ("weekly_warn_usd", 50.0),
        ("per_session_cap_usd", 1.5),
        ("monthly_budget_usd", 100.0),
    ]:
        result = _set_cap_handler(net_path, "team_bot_a", field, value)
        assert result["ok"] is True, (field, result)


def test_set_cap_rejects_inverted_ladder(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_handler
    _set_cap_handler(net_path, "team_bot_a", "l1_breaker_usd", 50.0)
    # L2 below L1 → reject
    result = _set_cap_handler(net_path, "team_bot_a", "l2_breaker_usd", 25.0)
    assert result["ok"] is False
    assert result.get("kind") == "remediation_ladder_inverted"


def test_set_cap_clears_with_none(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_handler
    _set_cap_handler(net_path, "team_bot_a", "tier_downgrade_usd", 8.0)
    result = _set_cap_handler(net_path, "team_bot_a", "tier_downgrade_usd", None)
    assert result["ok"] is True


def test_set_cap_cache_retention_accepts_enum(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_handler
    result = _set_cap_handler(net_path, "team_bot_a", "cache_retention", "long")
    assert result["ok"] is True


def test_set_cap_rejects_unknown_field(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_handler
    result = _set_cap_handler(net_path, "team_bot_a", "made_up_field", 5.0)
    assert result["ok"] is False
    assert "unknown field" in result["error"]


def test_set_cap_validate_rejects_negative(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_validate
    result = _set_cap_validate(net_path, "team_bot_a", "l1_breaker_usd", -5)
    assert result["ok"] is False


def test_set_cap_validate_rejects_bad_cache_retention(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _set_cap_validate
    result = _set_cap_validate(net_path, "team_bot_a", "cache_retention", "forever")
    assert result["ok"] is False


# ─── action.cost.reset_remediation ────────────────────────────────────────


def test_reset_remediation_tier_downgrade_removes_flag(pod_env):
    shared, net_path = pod_env
    from datetime import date
    flag_dir = shared / "cost_remediations" / "team_bot_a"
    flag_dir.mkdir(parents=True)
    (flag_dir / "tier_downgrade.flag").write_text(str(date.today()))
    from evolve_admin.evo.tools.action_cost import _reset_remediation_handler
    result = _reset_remediation_handler(net_path, "team_bot_a", "tier_downgrade")
    assert result["ok"] is True
    assert result["was_active"] is True
    assert not (flag_dir / "tier_downgrade.flag").exists()


def test_reset_remediation_tier_downgrade_when_not_active(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _reset_remediation_handler
    result = _reset_remediation_handler(net_path, "team_bot_a", "tier_downgrade")
    assert result["ok"] is True
    assert result["was_active"] is False


def test_reset_remediation_rejects_unknown_level(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _reset_remediation_handler
    result = _reset_remediation_handler(net_path, "team_bot_a", "made_up")
    assert result["ok"] is False
    assert "level" in result["error"]


def test_reset_remediation_validate_rejects_unknown_bot(pod_env):
    _shared, net_path = pod_env
    from evolve_admin.evo.tools.action_cost import _reset_remediation_validate
    result = _reset_remediation_validate(net_path, "no_such_bot", "tier_downgrade")
    assert result["ok"] is False
