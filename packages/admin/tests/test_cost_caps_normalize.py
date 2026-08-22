"""Tests for evolve_admin.migrations.cost_caps_normalize.

The migration:
  1. Moves ``network.json::bots.<bot>.daily_cap_usd`` →
     ``better-engine-config.json::bots.<bot>.budget.per_bot_daily_hard_usd``
  2. Moves sandbox override ``openclaw.agents.defaults.sessionBudgetCapUsd`` →
     ``better-engine-config.json::bots.<bot>.budget.per_bot_session_cost_cap_usd``
  3. Moves sandbox override ``openclaw.agents.defaults.models.cacheRetention`` →
     ``better-engine-config.json::bots.<bot>.budget.per_bot_cache_retention``
  4. Strips the legacy keys after copying.
  5. Is idempotent (re-running with no legacy keys is a no-op).
  6. When canonical and legacy both carry a value, canonical wins (the
     legacy key is still stripped — both can't coexist).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def _be_config_importable():
    """Ensure ``better_engine_config`` is importable inside the migration."""
    analyzer_dir = Path(__file__).resolve().parents[2] / "analyzer"
    added = False
    if str(analyzer_dir) not in sys.path:
        sys.path.insert(0, str(analyzer_dir))
        added = True
    yield
    if added:
        try:
            sys.path.remove(str(analyzer_dir))
        except ValueError:
            pass
        sys.modules.pop("better_engine_config", None)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    return p


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _write_network(network_path: Path, payload: dict) -> None:
    network_path.write_text(json.dumps(payload))


def _write_be_config(shared_dir: Path, payload: dict) -> None:
    (shared_dir / "better-engine-config.json").write_text(json.dumps(payload))


def _read_network(network_path: Path) -> dict:
    return json.loads(network_path.read_text())


def _read_be_config(shared_dir: Path) -> dict:
    return json.loads((shared_dir / "better-engine-config.json").read_text())


# ── network.json::daily_cap_usd → BE config ─────────────────────────────────


def test_migrates_daily_cap_from_network_to_be_config(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {
        "bots": {"team_bot_a": {"role": "member", "daily_cap_usd": 12.50}},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "team_bot_a" in result.daily_hard_cap_migrated
    assert "team_bot_a" in result.daily_hard_cap_stripped
    # Legacy key gone from network.json.
    assert "daily_cap_usd" not in _read_network(network_path)["bots"]["team_bot_a"]
    # Value lives in BE config.
    be = _read_be_config(shared_dir)
    assert be["bots"]["team_bot_a"]["budget"]["per_bot_daily_hard_usd"] == 12.50


def test_strips_legacy_cap_when_be_config_already_has_value(
    shared_dir, network_path, _be_config_importable,
):
    """If BE config carries a per-bot daily hard cap, the migration must NOT
    overwrite it — operator's most recent write via the new endpoint wins.
    But the legacy key is still stripped (no soak window)."""
    _write_network(network_path, {
        "bots": {"team_bot_a": {"role": "member", "daily_cap_usd": 12.50}},
    })
    _write_be_config(shared_dir, {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"team_bot_a": {"budget": {"per_bot_daily_hard_usd": 50.00}}},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    # BE wins over legacy: not migrated (already set), but still stripped.
    assert "team_bot_a" not in result.daily_hard_cap_migrated
    assert "team_bot_a" in result.daily_hard_cap_stripped
    be = _read_be_config(shared_dir)
    assert be["bots"]["team_bot_a"]["budget"]["per_bot_daily_hard_usd"] == 50.00
    assert "daily_cap_usd" not in _read_network(network_path)["bots"]["team_bot_a"]


def test_zero_or_negative_legacy_cap_is_treated_as_absent(
    shared_dir, network_path, _be_config_importable,
):
    """Legacy convention: 0 / ≤ 0 means 'no cap'. Don't copy, but do strip."""
    _write_network(network_path, {
        "bots": {"team_bot_a": {"role": "member", "daily_cap_usd": 0}},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "team_bot_a" not in result.daily_hard_cap_migrated
    assert "team_bot_a" in result.daily_hard_cap_stripped
    assert "daily_cap_usd" not in _read_network(network_path)["bots"]["team_bot_a"]


def test_idempotent_on_clean_state(
    shared_dir, network_path, _be_config_importable,
):
    """No legacy keys anywhere → no-op (no file rewrites, no errors)."""
    _write_network(network_path, {"bots": {"team_bot_a": {"role": "member"}}})
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.total_changes == 0
    assert result.errors == []
    # better-engine-config.json never got written (nothing to migrate).
    assert not (shared_dir / "better-engine-config.json").exists()


def test_re_run_after_migration_is_noop(
    shared_dir, network_path, _be_config_importable,
):
    """First run migrates; second run sees clean state and does nothing."""
    _write_network(network_path, {
        "bots": {"team_bot_a": {"role": "member", "daily_cap_usd": 8.00}},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    first = run(shared_dir, network_path)
    assert first.total_changes > 0
    second = run(shared_dir, network_path)
    assert second.total_changes == 0
    assert second.errors == []


def test_multiple_bots_processed_independently(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {
        "bots": {
            "team_bot_a": {"role": "member", "daily_cap_usd": 5.0},
            "team_bot_b": {"role": "member", "daily_cap_usd": 10.0},
            "team_bot_c": {"role": "member"},  # no legacy key
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert sorted(result.daily_hard_cap_migrated) == ["team_bot_a", "team_bot_b"]
    assert sorted(result.daily_hard_cap_stripped) == ["team_bot_a", "team_bot_b"]
    assert result.bots_inspected == 3
    net = _read_network(network_path)
    for bot in ("team_bot_a", "team_bot_b", "team_bot_c"):
        assert "daily_cap_usd" not in net["bots"][bot]
    be = _read_be_config(shared_dir)
    assert be["bots"]["team_bot_a"]["budget"]["per_bot_daily_hard_usd"] == 5.0
    assert be["bots"]["team_bot_b"]["budget"]["per_bot_daily_hard_usd"] == 10.0


# ── Sandbox-override session_cap + cache_retention → BE config ───────────────
#
# The two TunableKey schema entries were deleted in Phase 4b, so write_override
# refuses to seed test state via the normal path. The helper below writes the
# legacy sandbox-overrides file shape directly — same format read_bot_overrides
# parses — so we can pin the migration's read+strip behavior against historical
# state without resurrecting the schema entries.


def _seed_raw_sandbox_override(
    shared_dir: Path, bot_id: str, key: str, value, *, set_by: str = "legacy",
) -> None:
    """Hand-write a sandbox-overrides file in the on-disk shape. Bypasses
    write_override's schema check so we can stage entries for schema paths
    the production code no longer recognizes."""
    overrides_dir = shared_dir / "sandbox" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    path = overrides_dir / f"{bot_id}.json"
    if path.exists():
        existing = json.loads(path.read_text())
    else:
        existing = {"bot_id": bot_id, "overrides": {}}
    existing["overrides"][key] = {
        "value": value,
        "set_by": set_by,
        "set_at": "2026-05-01T00:00:00Z",
    }
    path.write_text(json.dumps(existing))


def test_migrates_session_cap_from_sandbox(
    shared_dir, network_path, fixed_now, _be_config_importable,
):
    _write_network(network_path, {"bots": {"team_bot_a": {"role": "member"}}})
    _seed_raw_sandbox_override(
        shared_dir, "team_bot_a",
        "openclaw.agents.defaults.sessionBudgetCapUsd",
        7.25, set_by="operator",
    )
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "team_bot_a" in result.session_cap_migrated
    assert "team_bot_a" in result.session_cap_stripped
    # BE config carries the value.
    be = _read_be_config(shared_dir)
    assert (
        be["bots"]["team_bot_a"]["budget"]["per_bot_session_cost_cap_usd"] == 7.25
    )
    # Sandbox override gone.
    from evolve_admin.config_sandbox import read_bot_overrides
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert "openclaw.agents.defaults.sessionBudgetCapUsd" not in bo.overrides


def test_migrates_cache_retention_from_sandbox(
    shared_dir, network_path, fixed_now, _be_config_importable,
):
    _write_network(network_path, {"bots": {"team_bot_a": {"role": "member"}}})
    _seed_raw_sandbox_override(
        shared_dir, "team_bot_a",
        "openclaw.agents.defaults.models.cacheRetention",
        "long", set_by="operator",
    )
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "team_bot_a" in result.cache_retention_migrated
    assert "team_bot_a" in result.cache_retention_stripped
    be = _read_be_config(shared_dir)
    assert (
        be["bots"]["team_bot_a"]["budget"]["per_bot_cache_retention"] == "long"
    )
    from evolve_admin.config_sandbox import read_bot_overrides
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert "openclaw.agents.defaults.models.cacheRetention" not in bo.overrides


def test_sandbox_override_does_not_overwrite_be_config_value(
    shared_dir, network_path, fixed_now, _be_config_importable,
):
    """BE config already has a session cap → sandbox value is stripped but
    NOT copied (canonical wins)."""
    (shared_dir / "better-engine-config.json").write_text(json.dumps({
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"team_bot_a": {"budget": {"per_bot_session_cost_cap_usd": 3.00}}},
    }))
    _write_network(network_path, {"bots": {"team_bot_a": {"role": "member"}}})
    _seed_raw_sandbox_override(
        shared_dir, "team_bot_a",
        "openclaw.agents.defaults.sessionBudgetCapUsd",
        99.99, set_by="legacy",
    )
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "team_bot_a" not in result.session_cap_migrated
    assert "team_bot_a" in result.session_cap_stripped
    be = _read_be_config(shared_dir)
    assert (
        be["bots"]["team_bot_a"]["budget"]["per_bot_session_cost_cap_usd"] == 3.00
    )


def test_summary_line_describes_changes(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {
        "bots": {"team_bot_a": {"role": "member", "daily_cap_usd": 5.0}},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    line = result.summary_line()
    assert "1 bots inspected" in line
    assert "migrated for 1" in line
    assert "stripped from 1" in line


def test_summary_line_describes_noop(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {"bots": {"team_bot_a": {"role": "member"}}})
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert "no-op" in result.summary_line()


def test_corrupt_network_recorded_as_error(
    shared_dir, network_path, _be_config_importable,
):
    """Malformed network.json → migration records error and returns,
    rather than crashing the admin server boot."""
    network_path.write_text("{not valid json")
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.errors


# ── Phase 8: pod-wide thresholds → BE config pod_defaults ─────────────────


def test_pod_thresholds_migrate_to_be_config(
    shared_dir, network_path, _be_config_importable,
):
    """network.json::thresholds.{daily/weekly}* → BE config pod_defaults.budget."""
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "dailySpendAlertUsd": 5.0,
            "weeklySpendAlertUsd": 20.0,
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.pod_daily_hard_migrated is True
    assert result.pod_daily_warn_migrated is True
    assert result.pod_weekly_warn_migrated is True
    assert result.pod_thresholds_stripped is True
    be = _read_be_config(shared_dir)
    pod = be["pod_defaults"]["budget"]
    assert pod["per_bot_daily_hard_usd"] == 50.0
    assert pod["per_bot_daily_warn_usd"] == 5.0
    assert pod["pod_weekly_warn_usd"] == 20.0
    # Cost keys gone from network.json; thresholds dict gone since it
    # held only the four cost fields.
    net = _read_network(network_path)
    assert "thresholds" not in net


def test_pod_thresholds_spend_cap_action_downgrade_maps_to_tier_downgrade(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "spendCapAction": "downgrade-tier",
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.pod_tier_downgrade_migrated is True
    be = _read_be_config(shared_dir)
    assert be["pod_defaults"]["budget"]["tier_downgrade_usd"] == 50.0


def test_pod_thresholds_spend_cap_action_suspend_maps_to_l2(
    shared_dir, network_path, _be_config_importable,
):
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "spendCapAction": "suspend-bot",
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.pod_l2_breaker_migrated is True
    be = _read_be_config(shared_dir)
    assert be["pod_defaults"]["budget"]["l2_breaker_usd"] == 50.0


def test_pod_thresholds_spend_cap_action_alert_only_is_noop(
    shared_dir, network_path, _be_config_importable,
):
    """alert-only means the L1 cap fires the warn alert; no extra remediation."""
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "spendCapAction": "alert-only",
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.pod_tier_downgrade_migrated is False
    assert result.pod_l2_breaker_migrated is False
    be = _read_be_config(shared_dir)
    pod = be["pod_defaults"]["budget"]
    # Compiled defaults seed these as None; the migration must not write
    # a positive value to either.
    assert pod.get("tier_downgrade_usd") is None
    assert pod.get("l2_breaker_usd") is None


def test_pod_thresholds_spend_cap_action_pause_crons_drops(
    shared_dir, network_path, _be_config_importable,
):
    """pause-crons is subsumed by L1 in the new spec; just drop it."""
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "spendCapAction": "pause-crons",
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    result = run(shared_dir, network_path)
    assert result.pod_l2_breaker_migrated is False
    assert result.pod_tier_downgrade_migrated is False
    # daily_hard still migrated
    assert result.pod_daily_hard_migrated is True


def test_pod_thresholds_non_cost_fields_preserved(
    shared_dir, network_path, _be_config_importable,
):
    """thresholds dict keeps non-cost fields like burst settings."""
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "burstAlertUsd": 5.0,
            "burstAlertWindowMin": 60,
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    run(shared_dir, network_path)
    net = _read_network(network_path)
    # dailySpendCapUsd stripped; burst fields kept.
    assert net["thresholds"]["burstAlertUsd"] == 5.0
    assert net["thresholds"]["burstAlertWindowMin"] == 60
    assert "dailySpendCapUsd" not in net["thresholds"]


def test_pod_thresholds_does_not_overwrite_existing_be_values(
    shared_dir, network_path, _be_config_importable,
):
    """If operator has already set pod_defaults via /api/arbiter/pod-defaults,
    the migration must NOT clobber those values with legacy ones."""
    _write_be_config(shared_dir, {
        "schema_version": 1,
        "pod_defaults": {
            "budget": {
                "tier_downgrade_usd": 99.0,  # explicit operator choice
            },
        },
        "bots": {},
    })
    _write_network(network_path, {
        "bots": {},
        "thresholds": {
            "dailySpendCapUsd": 50.0,
            "spendCapAction": "downgrade-tier",
        },
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    run(shared_dir, network_path)
    be = _read_be_config(shared_dir)
    # Existing tier_downgrade preserved; legacy NOT overwritten.
    assert be["pod_defaults"]["budget"]["tier_downgrade_usd"] == 99.0


def test_pod_thresholds_idempotent(
    shared_dir, network_path, _be_config_importable,
):
    """Second run with empty thresholds is a no-op."""
    _write_network(network_path, {
        "bots": {},
        "thresholds": {"dailySpendCapUsd": 50.0},
    })
    from evolve_admin.migrations.cost_caps_normalize import run
    first = run(shared_dir, network_path)
    assert first.pod_daily_hard_migrated is True
    second = run(shared_dir, network_path)
    assert second.pod_daily_hard_migrated is False
    assert second.total_changes == 0
    assert second.bots_inspected == 0
