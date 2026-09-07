"""Regression tests for heal pattern-detection — gateway_instability watchdog
events instead of investigation proposals.

Pins the contract that heal.py emits a single aggregated WatchdogEvent for
gateway-down patterns rather than one investigation Proposal per bot. The
near-duplicate proposal cards (six bots → six identical "Manual investigation
needed" cards) are the bug being fixed; this test guards against regression.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402
from generators.evolve_watchdog.events import read_events_range  # noqa: E402


def _write_incident(shared_dir: Path, bot_id: str, when: datetime, type_: str) -> None:
    day = when.strftime("%Y-%m-%d")
    day_dir = shared_dir / "incidents" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = when.strftime("%H%M%S%f")
    out = day_dir / f"{bot_id}-{ts}-{type_}.json"
    out.write_text(json.dumps({
        "bot_id": bot_id,
        "detected_at": when.isoformat().replace("+00:00", "Z"),
        "type": type_,
        "detail": "synthetic test incident",
        "port": 19000,
    }))


def _read_all_watchdog_events(shared_dir: Path) -> list:
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    return list(read_events_range(shared_dir, start, end))


def test_check_patterns_emits_aggregated_gateway_instability(tmp_path):
    """When multiple bots cross the down-threshold in the same window, heal
    emits ONE pod-wide gateway_instability event with bots in details."""
    shared_dir = tmp_path
    now = datetime.now(timezone.utc)

    # Three bots over threshold (3), one bot at threshold-1 → not affected.
    for bot_id, count in [("team_bot_a", 5), ("evolve", 4), ("team_bot_c", 3), ("personal_bot", 2)]:
        for i in range(count):
            _write_incident(shared_dir, bot_id, now - timedelta(minutes=10 * i), "gateway_down")

    statuses = [
        heal.BotStatus(bot_id=b, port=19000, healthy=False, response_time_ms=None)
        for b in ("team_bot_a", "evolve", "team_bot_c", "personal_bot")
    ]
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg={
        "windowHours": 24, "failuresBeforeProposal": 3,
    })

    events = _read_all_watchdog_events(shared_dir)
    instability = [e for e in events if e.event_type == "gateway_instability"]
    assert len(instability) == 1, f"expected one aggregated event, got {len(instability)}"

    event = instability[0]
    assert event.bot_id is None, "gateway_instability is pod-wide, not per-bot"
    assert event.severity == "alert"
    assert set(event.details["bots"].keys()) == {"team_bot_a", "evolve", "team_bot_c"}, (
        "personal_bot was below threshold and must not appear in the event"
    )
    assert event.details["bots"]["team_bot_a"]["gateway_down"] == 5
    assert event.details["window_hours"] == 24
    assert event.details["threshold"] == 3


def test_check_patterns_does_not_write_investigation_proposals(tmp_path):
    """The screenshot-bug regression test: no proposal-shaped JSON should land
    under proposals/pending/ from heal pattern detection."""
    shared_dir = tmp_path
    now = datetime.now(timezone.utc)
    for i in range(5):
        _write_incident(shared_dir, "team_bot_a", now - timedelta(minutes=i), "gateway_down")

    statuses = [heal.BotStatus(bot_id="team_bot_a", port=19000, healthy=False, response_time_ms=None)]
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg={
        "windowHours": 24, "failuresBeforeProposal": 3,
    })

    pending_dir = shared_dir / "proposals" / "pending"
    if pending_dir.exists():
        assert list(pending_dir.glob("*.json")) == [], (
            "heal must no longer write investigation proposals; that pattern was "
            "filling the approval queue with non-actionable cards"
        )


def test_check_patterns_dedupes_within_window(tmp_path):
    """Calling _check_patterns twice in quick succession produces one event,
    not two — the dedup window suppresses repeats."""
    shared_dir = tmp_path
    now = datetime.now(timezone.utc)
    for i in range(5):
        _write_incident(shared_dir, "team_bot_a", now - timedelta(minutes=i), "gateway_down")

    statuses = [heal.BotStatus(bot_id="team_bot_a", port=19000, healthy=False, response_time_ms=None)]
    cfg = {"windowHours": 24, "failuresBeforeProposal": 3}
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg=cfg)
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg=cfg)

    events = _read_all_watchdog_events(shared_dir)
    instability = [e for e in events if e.event_type == "gateway_instability"]
    assert len(instability) == 1


def test_check_patterns_no_incidents_no_event(tmp_path):
    """No incidents over threshold → no event written. Quiet is good."""
    shared_dir = tmp_path
    statuses = [heal.BotStatus(bot_id="team_bot_a", port=19000, healthy=True, response_time_ms=10)]
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg={
        "windowHours": 24, "failuresBeforeProposal": 3,
    })
    events = _read_all_watchdog_events(shared_dir)
    assert events == []


# ── Phase 6: last-error enrichment on the gateway_instability signal ─────────


def test_last_gateway_error_finds_recent_error_line(tmp_path):
    """_last_gateway_error returns the most recent error+ts from the log."""
    from datetime import datetime, timezone
    err_path = tmp_path / "gateway.err.log"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    err_path.write_text(
        f"{now} [memory] embeddings rate limited; retrying in 547ms\n"
        f"{now} [memory] sync failed (session-start): Error: openai embeddings failed: 429\n"
        f"{now} [fetch-timeout] fetch timeout reached; aborting operation\n"
    )
    result = heal._last_gateway_error(err_path)
    assert result is not None
    line, ts = result
    assert "fetch timeout" in line


def test_last_gateway_error_returns_none_on_missing_file(tmp_path):
    """A bot with no gateway.err.log is fine — return None, don't raise."""
    assert heal._last_gateway_error(tmp_path / "does-not-exist.log") is None


def test_last_gateway_error_returns_none_on_no_errors(tmp_path):
    """A log full of normal startup lines yields no enrichment."""
    err_path = tmp_path / "gateway.err.log"
    err_path.write_text("[agent/embedded] [trace] startup stages\n[memory] cache primed\n")
    assert heal._last_gateway_error(err_path) is None


def test_check_patterns_enriches_signal_with_last_error(tmp_path, monkeypatch):
    """The aggregated gateway_instability signal now carries last_error per bot
    when the log is readable — addresses the 'just says to check the logs'
    actionability gap."""
    from datetime import datetime, timezone

    shared_dir = tmp_path
    now = datetime.now(timezone.utc)

    # 4 down incidents for security_bot → over threshold
    for i in range(4):
        _write_incident(shared_dir, "security_bot", now - timedelta(minutes=10 * i), "gateway_down")

    # Write a synthetic gateway.err.log and redirect the path resolver.
    err_log = tmp_path / "security_bot-gateway.err.log"
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    err_log.write_text(
        f"{ts} [memory] sync failed: Error: openai embeddings failed: 429\n"
        f"{ts} [fetch-timeout] fetch timeout reached; aborting operation\n"
    )
    monkeypatch.setattr(heal, "_gateway_err_log_path",
                        lambda bid, _cfg: err_log if bid == "security_bot" else tmp_path / "missing.log")

    statuses = [heal.BotStatus(bot_id="security_bot", port=19000, healthy=False, response_time_ms=None)]
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg={
        "windowHours": 24, "failuresBeforeProposal": 3,
    })

    events = _read_all_watchdog_events(shared_dir)
    instability = [e for e in events if e.event_type == "gateway_instability"]
    assert len(instability) == 1

    bots = instability[0].details["bots"]
    assert "security_bot" in bots
    assert "last_error" in bots["security_bot"], (
        "gateway_instability should carry the most recent gateway error "
        "so the operator doesn't have to ssh in to diagnose"
    )
    assert "fetch timeout" in bots["security_bot"]["last_error"]
    # Guidance should mention embeddings since the error line surfaced.
    assert "embedding" in instability[0].details["guidance"].lower()


def test_check_patterns_falls_back_when_log_unreadable(tmp_path, monkeypatch):
    """If the log is missing/unreadable, the signal still emits with just
    the counts (no crash, no missing fields beyond last_error)."""
    shared_dir = tmp_path
    now = datetime.now(timezone.utc)
    for i in range(3):
        _write_incident(shared_dir, "admin_bot", now - timedelta(minutes=i), "gateway_down")
    monkeypatch.setattr(heal, "_gateway_err_log_path",
                        lambda _bid, _cfg: tmp_path / "nonexistent.log")

    statuses = [heal.BotStatus(bot_id="admin_bot", port=19000, healthy=False, response_time_ms=None)]
    heal._check_patterns(statuses, shared_dir, config={}, heal_cfg={
        "windowHours": 24, "failuresBeforeProposal": 3,
    })
    events = _read_all_watchdog_events(shared_dir)
    instability = [e for e in events if e.event_type == "gateway_instability"]
    assert len(instability) == 1
    bots = instability[0].details["bots"]
    assert "last_error" not in bots["admin_bot"], "no enrichment when log absent"
    # Guidance falls back to the generic line in the no-error path.
    assert "process supervisor" in instability[0].details["guidance"].lower()


def test_emit_watchdog_alert_per_bot_dedup_independent(tmp_path):
    """config_drift_unexplained is per-bot — different bots' drift should
    each get its own event, but the same bot twice should dedup."""
    shared_dir = tmp_path

    def emit(bot_id):
        return heal._emit_watchdog_alert(
            shared_dir=shared_dir,
            event_type="config_drift_unexplained",
            severity="alert",
            bot_id=bot_id,
            details={"drifts": ["bind: 0.0.0.0 vs 127.0.0.1"]},
            dedup_window_hours=24,
        )

    assert emit("team_bot_a") is True
    assert emit("team_bot_a") is False, "second call for same bot should dedup"
    assert emit("evolve") is True, "different bot must not be deduped against team_bot_a"

    events = _read_all_watchdog_events(shared_dir)
    drift = [e for e in events if e.event_type == "config_drift_unexplained"]
    assert {e.bot_id for e in drift} == {"team_bot_a", "evolve"}
