"""tests/test_host_health_sleep.py — Phase 8.2 sleep-gap detection.

A dedicated host must never sleep: launchd jobs don't fire and gateways
are suspended, so a sleep gap means the pod was silently dark. Covers the
kern.sleeptime/kern.waketime posture classifier and the host_slept Signal
(fires while the last sleep is <24h old, sweep-resolves after).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import host_health  # noqa: E402


def _stub_kern(monkeypatch, sleep_t: float, wake_t: float) -> None:
    monkeypatch.setattr(host_health, "_sleep_cache", None)
    monkeypatch.setattr(
        host_health, "_read_kern_timeval",
        lambda name: sleep_t if name == "kern.sleeptime" else wake_t,
    )


NOW = 1_781_100_000.0


class TestSleepPosture:
    def test_never_slept_since_boot(self, monkeypatch):
        _stub_kern(monkeypatch, 0.0, 0.0)
        posture = host_health.sleep_posture(now=NOW)
        assert posture["status"] == "ok"
        assert posture["slept_recently"] is False
        assert posture["last_sleep_at"] is None

    def test_recent_long_sleep_alerts(self, monkeypatch):
        # Slept 10 minutes, woke an hour ago.
        _stub_kern(monkeypatch, NOW - 4200, NOW - 3600)
        posture = host_health.sleep_posture(now=NOW)
        assert posture["status"] == "alert"
        assert posture["last_sleep_seconds"] == 600

    def test_short_blip_ignored(self, monkeypatch):
        _stub_kern(monkeypatch, NOW - 3660, NOW - 3600)  # 60s < 2-min floor
        assert host_health.sleep_posture(now=NOW)["status"] == "ok"

    def test_old_sleep_outside_window_is_ok(self, monkeypatch):
        woke = NOW - 25 * 3600  # 25h ago, past the 24h report window
        _stub_kern(monkeypatch, woke - 600, woke)
        assert host_health.sleep_posture(now=NOW)["status"] == "ok"

    def test_cache_serves_within_ttl(self, monkeypatch):
        _stub_kern(monkeypatch, NOW - 4200, NOW - 3600)
        first = host_health.sleep_posture(now=NOW)
        # Change the stub; within TTL the cached posture is returned.
        monkeypatch.setattr(host_health, "_read_kern_timeval", lambda name: 0.0)
        assert host_health.sleep_posture(now=NOW + 5) is first


def _snapshot(sleep_status: str) -> dict:
    now = time.time()
    return {
        "ok": True,
        "available": True,
        "hostname": "testhost",
        "sleep": {
            "last_sleep_at": now - 4200,
            "last_wake_at": now - 3600,
            "last_sleep_seconds": 600,
            "slept_recently": sleep_status == "alert",
            "status": sleep_status,
        },
    }


def _signals_by_type(shared_dir: Path, subdir: str) -> dict[str, dict]:
    out = {}
    for f in (shared_dir / "signals" / subdir).glob("*.json"):
        data = json.loads(f.read_text())
        out[data["type"]] = data
    return out


class TestHostSleptSignal:
    def test_fires_and_sweep_resolves(self, tmp_path):
        # Sleep gap observed → host_slept fires.
        host_health.emit_signals_from_snapshot(_snapshot("alert"), tmp_path)
        firing = _signals_by_type(tmp_path, "firing")
        assert "host_slept" in firing
        sig = firing["host_slept"]
        assert sig["producer"] == "host_health"
        assert sig["severity"] == "alert"
        assert "pmset -c sleep 0 displaysleep 0" in sig["body"]
        assert sig["details"]["last_sleep_seconds"] == 600

        # Next snapshot with no recent sleep → the sweep auto-resolves it.
        host_health.emit_signals_from_snapshot(_snapshot("ok"), tmp_path)
        assert "host_slept" not in _signals_by_type(tmp_path, "firing")
        archived = _signals_by_type(tmp_path, "archived")
        assert archived.get("host_slept", {}).get("state") == "resolved"

    def test_reobserve_dedups(self, tmp_path):
        host_health.emit_signals_from_snapshot(_snapshot("alert"), tmp_path)
        host_health.emit_signals_from_snapshot(_snapshot("alert"), tmp_path)
        firing = (tmp_path / "signals" / "firing").glob("*.json")
        host_slept = [json.loads(f.read_text()) for f in firing
                      if json.loads(f.read_text())["type"] == "host_slept"]
        assert len(host_slept) == 1
        assert host_slept[0]["observation_count"] == 2

    def test_unavailable_snapshot_is_noop(self, tmp_path):
        count = host_health.emit_signals_from_snapshot({"available": False}, tmp_path)
        assert count == 0
        assert not (tmp_path / "signals").exists()
