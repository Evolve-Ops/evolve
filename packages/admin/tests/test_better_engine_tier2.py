"""
Tests for Better Engine Tier 2: source adapters and urgent flag writing.

Run with:
    cd /Users/pod_admin/GitHub/evolve/.claude/worktrees/strange-lichterman
    python -m pytest packages/admin/tests/test_better_engine_tier2.py -v
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.better_engine.model import Recommendation, now_iso
# ComplianceAdapter was retired — all seven compliance issue types
# moved to compliance_scan-as-Signal-producer + three generators
# (manifest_quality, workspace_inventory, workspace_security). See:
# - packages/analyzer/tests/test_compliance_signal_emission.py
# - packages/analyzer/tests/test_manifest_quality_generator.py
# - packages/analyzer/tests/test_workspace_inventory_generator.py
# - packages/analyzer/tests/test_workspace_security_generator.py
#
# ScoreboardAdapter was retired previously — see git log for migration.


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_shared(tmp_path: Path) -> Path:
    """A minimal shared_dir tree for testing."""
    (tmp_path / "better-engine" / "cache").mkdir(parents=True)
    (tmp_path / "better-engine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scoreboard").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def network() -> dict:
    return {"members": ["team_bot_a", "admin_bot"]}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_metric(
    shared_dir: Path,
    bot_id: str,
    d: date,
    *,
    session_count: int = 10,
    turn_count: int = 50,
    maintenance_ratio: float = 0.20,
    total_cost_estimated: float = 0.50,
) -> None:
    """Drop a daily metrics file at the canonical path."""
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": 2,
        "bot_id": bot_id,
        "date": d.isoformat(),
        "session_count": session_count,
        "turn_count": turn_count,
        "maintenance_ratio": maintenance_ratio,
        "total_cost_estimated": total_cost_estimated,
    }))


def _write_app_manifest(shared_dir: Path, bot_id: str, app_id: str) -> None:
    p = shared_dir / "applications" / bot_id / f"{app_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": app_id, "name": app_id}))


# ComplianceAdapter tests were retired alongside the adapter — see the
# header comment of this file for the new test locations.


# ScoreboardAdapter tests were retired alongside the adapter. New coverage
# lives in:
#   - packages/analyzer/tests/test_cost_watchdog.py::test_maintenance_ratio_*
#   - packages/analyzer/tests/test_session_quality_generator.py
#   - packages/analyzer/tests/test_cost_watchdog.py::test_cost_spike_*
#   - packages/analyzer/tests/test_cost_spike_generator.py
# The applications rec is now covered exclusively by OnboardingAdapter
# (task scan_applications_{bot_id}); its tests live in this file in
# TestOnboardingAdapter (if/when added) or test_better_engine_tier0.py.


# ══════════════════════════════════════════════════════════════════════════════
# _flag_urgent_refresh() helper tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFlagUrgentRefresh:

    def _import_flag_fn(self, module: str):
        """Import _flag_urgent_refresh from the given module."""
        if module == "health":
            from evolve_admin.health import _flag_urgent_refresh
            return _flag_urgent_refresh
        elif module == "spend_alert":
            # Import from analyzer package path
            analyzer_dir = Path(__file__).parent.parent.parent.parent / "analyzer"
            sys.path.insert(0, str(analyzer_dir))
            import importlib
            sa = importlib.import_module("spend_alert")
            return sa._flag_urgent_refresh
        # (review.py's branch removed 2026-08-14 — the reviewer was retired
        # into arbiter/security_screen.py, which has no urgent-refresh flag.)
        raise ValueError(f"Unknown module: {module}")

    def test_health_flag_creates_file(self, tmp_path: Path) -> None:
        from evolve_admin.health import _flag_urgent_refresh
        _flag_urgent_refresh(tmp_path, source="health_check",
                             reason="gateway_fail", bot_id="team_bot_a")
        flag = tmp_path / "better-engine" / ".refresh-urgent"
        assert flag.exists()
        data = json.loads(flag.read_text())
        assert data["source"] == "health_check"
        assert data["reason"] == "gateway_fail"
        assert data["bot_id"] == "team_bot_a"
        assert "ts" in data

    def test_flag_file_is_valid_json(self, tmp_path: Path) -> None:
        from evolve_admin.health import _flag_urgent_refresh
        _flag_urgent_refresh(tmp_path, source="test", reason="test_reason")
        flag = tmp_path / "better-engine" / ".refresh-urgent"
        # Must be parseable JSON
        data = json.loads(flag.read_text())
        assert isinstance(data, dict)

    def test_flag_creates_parent_dirs(self, tmp_path: Path) -> None:
        # The better-engine dir does NOT exist yet
        shared = tmp_path / "nonexistent" / "shared"
        from evolve_admin.health import _flag_urgent_refresh
        _flag_urgent_refresh(shared, source="health_check", reason="gateway_fail")
        flag = shared / "better-engine" / ".refresh-urgent"
        assert flag.exists()

    def test_flag_kwargs_included(self, tmp_path: Path) -> None:
        from evolve_admin.health import _flag_urgent_refresh
        _flag_urgent_refresh(tmp_path, source="spend_alert",
                             reason="hard_cap_hit", bot_id="admin_bot",
                             extra_field="extra_value")
        flag = tmp_path / "better-engine" / ".refresh-urgent"
        data = json.loads(flag.read_text())
        assert data.get("extra_field") == "extra_value"


# ══════════════════════════════════════════════════════════════════════════════
# Dedup key exact format verification
# ══════════════════════════════════════════════════════════════════════════════

class TestDedupKeyFormats:
    """Verify exact dedup_key formats for each remaining adapter.

    The compliance:: dedup_key format was retired alongside ComplianceAdapter;
    compliance issues now flow through arbiter:: dedup_keys via three new
    generators (manifest_quality, workspace_inventory, workspace_security).
    The scoreboard:: dedup_key format was retired with ScoreboardAdapter
    previously.
    """

    # Placeholder to keep pytest happy with a class that has no remaining
    # tests but is documented; future per-adapter dedup-format tests should
    # land here as new adapters are added.
    pass

