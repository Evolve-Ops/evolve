"""heal.py honors L2 circuit breakers + TTL-reaps expired trips.

Spec: docs/spec-circuit-breakers-2026-05-21.md §3.5

Pinning two invariants at the heal layer:

  1. Per-bot OR pod-wide L2 trip → that bot's gateway is NOT restarted
     even when its health probe shows down. Same contract as the
     existing B2.e pause-state guard, extended to per-bot scope.

  2. heal acts as the TTL reaper: an L2 file whose expires_at has
     passed gets deleted on the next heal cycle, an "auto_recover"
     audit-log entry is written, and the bot stops being blocked
     (so its restart fires on the same cycle).

These tests mirror the structure of test_heal_recovery_pause.py — same
fixture, same stub layer, same canary (the restarts list).
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

import heal  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic env with two fake bots, both unhealthy.

    Stubs match test_heal_recovery_pause's env fixture, plus a second
    bot so we can verify per-bot scoping (blocking team_bot_a must not block
    security_bot).
    """
    restarts: list[str] = []

    monkeypatch.setattr(heal, "_suspend_guard", lambda *a, **k: False)
    monkeypatch.setattr(
        heal, "check_gateway",
        lambda bot_id, port, heal_cfg, **kw: heal.BotStatus(
            bot_id=bot_id, port=port, healthy=False,
            response_time_ms=None, error="stub-down",
        ),
    )

    def fake_restart(bot_id, os_user=None):
        restarts.append(bot_id)
        return True

    monkeypatch.setattr(heal, "restart_gateway", fake_restart)
    monkeypatch.setattr(heal, "_record_incident", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_alert_failure", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_check_patterns", lambda *a, **k: None)
    monkeypatch.setattr(heal, "detect_backup_drift", lambda *a, **k: [])
    monkeypatch.setattr(heal, "_write_status_file", lambda *a, **k: None)
    monkeypatch.setattr(heal, "check_pod_conduct_injection", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_in_restart_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_get_port", lambda *a, **k: 9001)

    config = {
        "primary": "team_bot_a",
        "members": ["team_bot_a", "security_bot"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "security_bot": {"user": "security_bot"}},
    }
    heal_cfg = {"restartCooldownMin": 0}

    return {
        "config": config,
        "heal_cfg": heal_cfg,
        "shared_dir": tmp_path,
        "restarts": restarts,
    }


def _write_breaker(
    shared_dir: Path, scope: str, *, expires_in_hours: float | None,
    breaker_type: str = "full",
) -> Path:
    """Helper: write an L2 breaker file for a scope.

    expires_in_hours=None → indefinite trip.
    Negative values → expired (for reaper tests).
    """
    path = shared_dir / "breakers" / scope / f"{breaker_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    payload = {
        "bot_id": scope,
        "type": breaker_type,
        "state": "tripped",
        "tripped_at": now.isoformat(),
        "expires_at": (
            None if expires_in_hours is None
            else (now + timedelta(hours=expires_in_hours)).isoformat()
        ),
        "initiated_by": "test",
        "reason": "synthetic",
        "trip_id": "trip-uuid",
        "motivating_signals": [],
    }
    path.write_text(json.dumps(payload))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot L2 trip
# ─────────────────────────────────────────────────────────────────────────────


class TestPerBotL2:
    def test_per_bot_trip_blocks_only_that_bot(self, env: dict) -> None:
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        # team_bot_a blocked; security_bot restarted
        assert env["restarts"] == ["security_bot"]

    def test_per_bot_trip_with_cost_type_does_not_block(
        self, env: dict,
    ) -> None:
        """L1 (cost) breakers don't block heal — gateway should be up
        and the plugin (Phase 3b) handles veto at turn-time."""
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=1,
                       breaker_type="cost")
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        # Both restarted — cost trips are NOT in heal's scope.
        assert sorted(env["restarts"]) == ["security_bot", "team_bot_a"]

    def test_indefinite_trip_blocks(self, env: dict) -> None:
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=None)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert env["restarts"] == ["security_bot"]


# ─────────────────────────────────────────────────────────────────────────────
# Pod-wide L2 trip
# ─────────────────────────────────────────────────────────────────────────────


class TestPodL2:
    def test_pod_trip_blocks_every_bot(self, env: dict) -> None:
        _write_breaker(env["shared_dir"], "pod", expires_in_hours=1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert env["restarts"] == [], "pod-wide L2 must block every restart"

    def test_pod_trip_plus_per_bot_trip_block(self, env: dict) -> None:
        """Both at once is fine — neither bot restarts."""
        _write_breaker(env["shared_dir"], "pod", expires_in_hours=1)
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert env["restarts"] == []


# ─────────────────────────────────────────────────────────────────────────────
# TTL reaper
# ─────────────────────────────────────────────────────────────────────────────


class TestReaper:
    def test_expired_trip_is_reaped(self, env: dict) -> None:
        """An expired trip gets its file deleted on this cycle."""
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        assert path.is_file()
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert not path.is_file(), "expired trip should have been reaped"

    def test_expired_trip_does_not_block_restart_same_cycle(
        self, env: dict,
    ) -> None:
        """After reap, the bot's restart proceeds on this same cycle —
        no 5-minute window of being down despite the trip having expired."""
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-2)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        # Reaped → not blocked → restarted alongside security_bot.
        assert sorted(env["restarts"]) == ["security_bot", "team_bot_a"]

    def test_reaper_writes_audit_log(self, env: dict) -> None:
        """Reaping writes an auto_recover entry to the audit log."""
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        log_dir = env["shared_dir"] / "breakers" / "log"
        log_files = list(log_dir.glob("*.jsonl"))
        assert log_files, "reaper must write an audit log entry"
        # Find the auto_recover entry.
        entries = []
        for fp in log_files:
            for line in fp.read_text().splitlines():
                if line.strip():
                    entries.append(json.loads(line))
        recovers = [e for e in entries if e.get("action") == "auto_recover"]
        assert len(recovers) == 1
        assert recovers[0]["scope"] == "team_bot_a"
        assert recovers[0]["type"] == "full"
        assert recovers[0]["initiated_by"] == "heal:reaper"

    def test_unexpired_trip_not_reaped(self, env: dict) -> None:
        """A still-valid trip stays on disk."""
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=2)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_indefinite_trip_never_reaped(self, env: dict) -> None:
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=None)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open on corrupt files
# ─────────────────────────────────────────────────────────────────────────────


class TestFailOpen:
    def test_corrupt_breaker_file_does_not_block(self, env: dict) -> None:
        path = env["shared_dir"] / "breakers" / "team_bot_a" / "full.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json{{{")

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        # Fail-open: team_bot_a still restarts.
        assert sorted(env["restarts"]) == ["security_bot", "team_bot_a"]
        # Corrupt file is NOT deleted (operator may want to inspect).
        assert path.is_file()

    def test_missing_breakers_dir_is_fine(self, env: dict) -> None:
        """No breakers/ dir at all → heal runs normally."""
        # tmp_path has no breakers/ subdir.
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert sorted(env["restarts"]) == ["security_bot", "team_bot_a"]

    def test_unparseable_expiry_treated_as_active(self, env: dict) -> None:
        """A malformed expires_at must NOT clear a trip — fail-safe direction."""
        path = env["shared_dir"] / "breakers" / "team_bot_a" / "full.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "bot_id": "team_bot_a", "type": "full", "state": "tripped",
            "expires_at": "garbage", "tripped_at": "?",
            "initiated_by": "test", "reason": "?", "trip_id": "x",
        }))
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        # team_bot_a is blocked (assumed active); only security_bot restarts.
        assert env["restarts"] == ["security_bot"]
        # And the file was NOT deleted (don't reap on malformed expiry).
        assert path.is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Co-existence with the legacy pause-state.json
# ─────────────────────────────────────────────────────────────────────────────


class TestPauseStateCoexistence:
    def test_legacy_pause_still_works(self, env: dict) -> None:
        """The B2.e pause-state.json contract is unchanged."""
        pause_flag = env["shared_dir"] / "recovery" / "pause-state.json"
        pause_flag.parent.mkdir(parents=True, exist_ok=True)
        pause_flag.write_text(json.dumps({
            "paused": True, "paused_at": "x", "reason": "legacy", "initiated_by": "test",
        }))
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert env["restarts"] == []

    def test_legacy_pause_or_breaker_either_blocks(self, env: dict) -> None:
        """If BOTH legacy pause AND a per-bot breaker exist, heal honors both."""
        pause_flag = env["shared_dir"] / "recovery" / "pause-state.json"
        pause_flag.parent.mkdir(parents=True, exist_ok=True)
        pause_flag.write_text(json.dumps({"paused": True, "paused_at": "x",
                                          "reason": "legacy", "initiated_by": "test"}))
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert env["restarts"] == []
