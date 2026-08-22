"""heal.py TTL-reaps expired cost-family breaker trips (L1 "cost" + "cost_l2").

Spec: docs/spec-circuit-breakers-2026-05-21.md §3.5.

Regression for the 2026-07-31 incident: _read_l2_breaker_state only ever
read full.json, so three auto:spend_alert L1 cost trips from early June
sat at state:"tripped" for 7+ weeks after their expires_at until the
operator manually ran `evolve-admin breaker reset <bot> cost`. The reap
must route through breakers_enforce.enforce_reset (restore the heartbeat
stash + exec-approvals stash) rather than bare-deleting the file —
otherwise a bot whose heartbeat was disabled at trip time stays
heartbeat-less forever after the reap.

Structure mirrors test_heal_breaker_aware.py — same fixture shape, same
stub layer. The stash-restore tests drive the REAL
breakers_enforce.enforce_reset against a synthetic home tree via
home_override (same harness as packages/admin/tests/test_breakers_enforce.py)
so "reaped and restored" is asserted end-to-end, not against a stub.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import heal  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic env with one fake bot, healthy (restarts aren't the
    canary here — the breaker files and stashes are)."""
    restarts: list[str] = []

    monkeypatch.setattr(heal, "_suspend_guard", lambda *a, **k: False)
    monkeypatch.setattr(
        heal, "check_gateway",
        lambda bot_id, port, heal_cfg, **kw: heal.BotStatus(
            bot_id=bot_id, port=port, healthy=True,
            response_time_ms=5.0, error=None,
        ),
    )
    monkeypatch.setattr(heal, "restart_gateway",
                        lambda bot_id, os_user=None: restarts.append(bot_id) or True)
    monkeypatch.setattr(heal, "_record_incident", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_alert_failure", lambda *a, **k: None)
    monkeypatch.setattr(heal, "_check_patterns", lambda *a, **k: None)
    monkeypatch.setattr(heal, "detect_backup_drift", lambda *a, **k: [])
    monkeypatch.setattr(heal, "_write_status_file", lambda *a, **k: None)
    monkeypatch.setattr(heal, "check_pod_conduct_injection", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_in_restart_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(heal, "_get_port", lambda *a, **k: 9001)
    # The seam caches on first success — reset so each test controls it.
    monkeypatch.setattr(heal, "_BREAKERS_ENFORCE_RESET", None)

    config = {
        "primary": "team_bot_a",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
    }
    return {
        "config": config,
        "heal_cfg": {"restartCooldownMin": 0},
        "shared_dir": tmp_path / "shared",
        "restarts": restarts,
    }


def _write_breaker(
    shared_dir: Path, scope: str, *, expires_in_hours: float | None,
    breaker_type: str = "cost",
) -> Path:
    """Write a breaker file. Negative expires_in_hours → expired;
    None → indefinite."""
    path = shared_dir / "breakers" / scope / f"{breaker_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    payload = {
        "bot_id": scope,
        "type": breaker_type,
        "state": "tripped",
        "tripped_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (
            None if expires_in_hours is None
            else (now + timedelta(hours=expires_in_hours)).isoformat()
        ),
        "initiated_by": "auto:spend_alert",
        "reason": "synthetic",
        "trip_id": "trip-uuid",
        "motivating_signals": [],
    }
    path.write_text(json.dumps(payload))
    return path


def _make_bot_home(tmp_path: Path, bot_id: str, *, every: str | None) -> Path:
    """Synthetic ~/.openclaw tree, post-trip state when every=None."""
    home = tmp_path / "homes" / bot_id
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True)
    heartbeat: dict[str, Any] = {"model": "anthropic/claude-haiku-4-5"}
    if every is not None:
        heartbeat["every"] = every
    (oc_dir / "openclaw.json").write_text(json.dumps(
        {"agents": {"defaults": {"heartbeat": heartbeat}}}, indent=2,
    ))
    return home


def _wire_real_enforce(monkeypatch, home: Path):
    """Point heal's enforce seam at the REAL breakers_enforce.enforce_reset,
    with home_override injected so no sudo/launchctl runs."""
    from evolve_admin import breakers_enforce

    def _reset(**kwargs):
        return breakers_enforce.enforce_reset(home_override=home, **kwargs)

    monkeypatch.setattr(heal, "_get_breakers_enforce_reset", lambda: _reset)
    return breakers_enforce


def _audit_entries(shared_dir: Path) -> list[dict]:
    out = []
    for fp in (shared_dir / "breakers" / "log").glob("*.jsonl"):
        for line in fp.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# (a) expired cost trip → reaped, heartbeat stash restored
# ─────────────────────────────────────────────────────────────────────────────


class TestExpiredCostReap:
    def test_expired_cost_trip_reaped_and_stash_restored(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every=None)
        _wire_real_enforce(monkeypatch, home)

        breaker = _write_breaker(env["shared_dir"], "team_bot_a",
                                 expires_in_hours=-1)
        stash = env["shared_dir"] / "breakers" / "team_bot_a" / "heartbeat-stash.json"
        stash.write_text(json.dumps({"every": "2h", "stashed_at": "x"}))

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)

        assert not breaker.is_file(), "expired cost trip must be reaped"
        assert not stash.is_file(), "stash must be consumed by the restore"
        oc = json.loads((home / ".openclaw" / "openclaw.json").read_text())
        assert oc["agents"]["defaults"]["heartbeat"]["every"] == "2h", (
            "heartbeat.every must be restored from the stash, not lost"
        )

    def test_reap_writes_auto_recover_audit_entry(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)

        recovers = [e for e in _audit_entries(env["shared_dir"])
                    if e.get("action") == "auto_recover"]
        assert len(recovers) == 1
        assert recovers[0]["scope"] == "team_bot_a"
        assert recovers[0]["type"] == "cost"
        assert recovers[0]["initiated_by"] == "heal:reaper"

    def test_unexpired_cost_trip_untouched(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=2)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_indefinite_cost_trip_never_reaped(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=None)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_expired_cost_l2_trip_reaped_with_bootstrap(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        """cost_l2 boots the gateway out at trip time; the reap must go
        through enforce_reset's bootstrap — heal's own restart_gateway
        (kickstart) can't resurrect a booted-out service."""
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        from evolve_admin import recovery
        from evolve_admin.recovery import PerBotResult

        bootstraps: list[str] = []

        def fake_bootstrap(bot_id: str, dry_run: bool) -> PerBotResult:
            bootstraps.append(bot_id)
            return PerBotResult(bot_id=bot_id, label="l", ok=True, rc=0,
                                stdout="", stderr="", elapsed_ms=1)

        monkeypatch.setattr(recovery, "_bootstrap_gateway", fake_bootstrap)
        path = _write_breaker(env["shared_dir"], "team_bot_a",
                              expires_in_hours=-1, breaker_type="cost_l2")

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)

        assert not path.is_file()
        assert bootstraps == ["team_bot_a"]


# ─────────────────────────────────────────────────────────────────────────────
# (b) defunct bot (gone from network.json) → residue still cleared
# ─────────────────────────────────────────────────────────────────────────────


class TestDefunctBotResidue:
    def test_defunct_bot_residue_cleared(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        """Real enforce_reset raises ValueError for a bot missing from
        network.json (seen live: "ledger" was removed from the pod but
        its breaker dir survived). The reaper must clear the file, both
        stashes, and the now-empty dir instead of erroring forever."""
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)

        scope_dir = env["shared_dir"] / "breakers" / "ledger"
        breaker = _write_breaker(env["shared_dir"], "ledger", expires_in_hours=-1)
        (scope_dir / "heartbeat-stash.json").write_text(
            json.dumps({"every": "2h", "stashed_at": "x"}))
        (scope_dir / "exec-approvals-stash.json").write_text(
            json.dumps({"exec_approvals": {}, "stashed_at": "x"}))

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)

        assert not breaker.is_file()
        assert not (scope_dir / "heartbeat-stash.json").is_file()
        assert not (scope_dir / "exec-approvals-stash.json").is_file()
        assert not scope_dir.exists(), "empty scope dir must be removed"
        recovers = [e for e in _audit_entries(env["shared_dir"])
                    if e.get("action") == "auto_recover"]
        assert len(recovers) == 1
        assert recovers[0]["scope"] == "ledger"
        assert "network.json" in recovers[0].get("note", "")

    def test_defunct_bot_dir_kept_when_other_breaker_active(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        """rmdir must not clobber a sibling breaker file that hasn't
        expired yet."""
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        scope_dir = env["shared_dir"] / "breakers" / "ledger"
        _write_breaker(env["shared_dir"], "ledger", expires_in_hours=-1)
        sibling = _write_breaker(env["shared_dir"], "ledger",
                                 expires_in_hours=None, breaker_type="full")

        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)

        assert sibling.is_file()
        assert scope_dir.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Fail-safe paths
# ─────────────────────────────────────────────────────────────────────────────


class TestReapFailSafe:
    def test_enforce_failure_leaves_trip_file_for_retry(
        self, env: dict, monkeypatch,
    ) -> None:
        """ok=False from enforce_reset (e.g. openclaw.json unreadable)
        must NOT delete the file — deleting would orphan the stash and
        leave the bot heartbeat-less forever."""
        from evolve_admin.breakers_enforce import EnforceResult

        def failing_reset(**kwargs):
            return EnforceResult(action="reset", scope=kwargs["scope"],
                                 breaker_type=kwargs["breaker_type"], ok=False)

        monkeypatch.setattr(heal, "_get_breakers_enforce_reset",
                            lambda: failing_reset)
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_enforce_unavailable_leaves_trip_file(
        self, env: dict, monkeypatch,
    ) -> None:
        monkeypatch.setattr(heal, "_get_breakers_enforce_reset", lambda: None)
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_dry_run_reaps_nothing(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, True)
        assert path.is_file()

    def test_reap_skipped_while_pod_paused(
        self, env: dict, tmp_path: Path, monkeypatch,
    ) -> None:
        """The reap kickstarts gateways via enforce_reset — under the
        operator's panic-button pause that would fight them."""
        home = _make_bot_home(tmp_path, "team_bot_a", every="2h")
        _wire_real_enforce(monkeypatch, home)
        pause = env["shared_dir"] / "recovery" / "pause-state.json"
        pause.parent.mkdir(parents=True, exist_ok=True)
        pause.write_text(json.dumps({"paused": True}))
        path = _write_breaker(env["shared_dir"], "team_bot_a", expires_in_hours=-1)
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert path.is_file()

    def test_full_breaker_reap_unchanged(
        self, env: dict, monkeypatch,
    ) -> None:
        """full.json stays owned by _read_l2_breaker_state — the cost
        reaper must skip it (no enforce_reset bootstrap; heal's own
        restart loop recovers the gateway on the same cycle)."""
        calls: list[str] = []
        monkeypatch.setattr(heal, "_get_breakers_enforce_reset",
                            lambda: lambda **kw: calls.append(kw["scope"]))
        path = _write_breaker(env["shared_dir"], "team_bot_a",
                              expires_in_hours=-1, breaker_type="full")
        heal.run_once(env["config"], env["shared_dir"], env["heal_cfg"], None, False)
        assert calls == [], "full.json must not route through enforce_reset"
        assert not path.is_file(), "L2 reap in _read_l2_breaker_state still fires"
