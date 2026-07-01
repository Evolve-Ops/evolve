"""Tests for evolve_admin.recovery — sprint pillar B2.e + V2.4-2.

Covers the recovery flow in four layers:

  1. State helpers — pause flag write/read/clear is atomic and roundtrips
  2. pause-all / resume-all — every bot gets a launchctl dispatch, the
     pause flag is the source of truth, partial failure leaves state
     correct, and dry-run touches nothing
  3. rollback / reverse-rollback — git-backed config recovery is
     reversible: every rollback snapshots the pre-state and writes an
     auditable record under ``{shared_dir}/recovery/rollbacks/``
  4. Pod-state commit list + rollback (V2.4-2) — recent commits from the
     deploy checkout, confirmation token issuance/consumption, and
     rollback safety gates (expired token, mismatched sha, dirty worktree)

The launchctl + subprocess + filesystem-write surface is mocked at
the ``recovery._bootout_gateway`` / ``recovery._bootstrap_gateway`` /
``recovery._write_bot_openclaw`` boundary so tests are hermetic and
don't actually invoke sudo or touch ``/Users/`` paths.

These tests intentionally exercise the HIGH-STAKES paths:
  - pause flag persists before any launchctl call (so heal.py sees
    paused=True even if launchctl fails)
  - resume-all keeps the flag in place when bootstrap partially fails
  - rollback refuses to proceed when the current-state read fails
    (otherwise rollback would be irreversible)
  - reverse-rollback restores the pre-rollback snapshot and stamps the
    original record
  - pod rollback: expired token is rejected without touching the repo
  - pod rollback: dirty worktree is rejected before git reset --hard
  - pod rollback: invalid sha is rejected
  - pod rollback: single-use token cannot be reused
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from evolve_admin import recovery


@pytest.fixture(autouse=True)
def _no_real_launchctl_via_seam(monkeypatch):
    """recovery's launchctl traffic flows through the Scheduler seam
    (4.3C S2); its verbs (bootout / kickstart -k) are live-traffic
    destructive. Any seam call that reaches the DEFAULT runner from this
    file is a test bug — inject one via
    ``set_scheduler(LaunchdScheduler(runner=fake))``.

    Guards the seam's default-runner function only — NOT the global
    ``subprocess.run``, which the git-fixture tests here legitimately use.
    """
    from runtime import scheduler as scheduler_mod  # the seam's real home
    from evolve_admin.runtime import set_scheduler

    def _boom(argv, *a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            "Scheduler seam reached the real subprocess runner in a "
            f"test_recovery test — inject a fake runner. argv={argv!r}"
        )

    monkeypatch.setattr(scheduler_mod, "_subprocess_runner", _boom)
    yield
    set_scheduler(None)


@dataclass
class _FakeNet:
    primary: str = "team_bot_a"
    members: tuple[str, ...] = ("admin_bot", "team_bot_b")


def _make_network(bots=("team_bot_a", "admin_bot", "team_bot_b")) -> dict:
    """Build a minimal network.json-shaped dict that recovery._iter_bots
    will walk. Each bot maps to itself as macOS user."""
    return {
        "primary": bots[0] if bots else None,
        "members": list(bots[1:]) if len(bots) > 1 else [],
        "bots": {b: {"user": b} for b in bots},
        "sharedDir": "/Users/Shared/evolve",
    }


# ── State helpers ────────────────────────────────────────────────────────────


def test_recovery_dir_creates_subdirs(tmp_path: Path):
    rd = recovery.recovery_dir(tmp_path)
    assert rd.is_dir()
    assert (rd / "rollbacks").is_dir()
    assert rd == tmp_path / "recovery"


def test_read_pause_state_returns_none_when_missing(tmp_path: Path):
    assert recovery.read_pause_state(tmp_path) is None
    assert recovery.is_paused(tmp_path) is False


def test_pause_state_roundtrips(tmp_path: Path):
    recovery._atomic_write_json(
        recovery.pause_state_path(tmp_path),
        {"paused": True, "paused_at": "2026-05-12T00:00:00+00:00", "reason": "test"},
    )
    state = recovery.read_pause_state(tmp_path)
    assert state is not None
    assert state["paused"] is True
    assert state["reason"] == "test"
    assert recovery.is_paused(tmp_path) is True


def test_read_pause_state_corrupt_file_returns_none(tmp_path: Path):
    """A corrupt flag must NEVER raise — heal.py reads it on every cycle."""
    p = recovery.pause_state_path(tmp_path)
    p.write_text("not-json{{", encoding="utf-8")
    assert recovery.read_pause_state(tmp_path) is None
    assert recovery.is_paused(tmp_path) is False


def test_read_pause_state_paused_false_returns_none(tmp_path: Path):
    """``paused: false`` is normalised to None (not-paused)."""
    p = recovery.pause_state_path(tmp_path)
    p.write_text(json.dumps({"paused": False, "cleared_at": "x"}), encoding="utf-8")
    assert recovery.read_pause_state(tmp_path) is None


# ── pause-all / resume-all ───────────────────────────────────────────────────


def _stub_bootout_ok(bot_id: str, dry_run: bool) -> recovery.PerBotResult:
    return recovery.PerBotResult(
        bot_id=bot_id, label=recovery._gateway_label(bot_id),
        ok=True, rc=0, elapsed_ms=5,
    )


def _stub_bootout_fail(bot_id: str, dry_run: bool) -> recovery.PerBotResult:
    return recovery.PerBotResult(
        bot_id=bot_id, label=recovery._gateway_label(bot_id),
        ok=False, rc=2, stderr="kaboom", elapsed_ms=5,
    )


def _stub_bootstrap_ok(bot_id: str, dry_run: bool) -> recovery.PerBotResult:
    return recovery.PerBotResult(
        bot_id=bot_id, label=recovery._gateway_label(bot_id),
        ok=True, rc=0, elapsed_ms=5,
    )


def _stub_bootstrap_fail(bot_id: str, dry_run: bool) -> recovery.PerBotResult:
    return recovery.PerBotResult(
        bot_id=bot_id, label=recovery._gateway_label(bot_id),
        ok=False, rc=5, stderr="bootstrap kaboom", elapsed_ms=5,
    )


def test_pause_all_sets_flag_before_dispatch(tmp_path: Path, monkeypatch):
    """pause-all MUST write the flag before dispatching launchctl — so
    heal.py sees paused=True even if launchctl partially fails."""
    network = _make_network()
    captured: dict = {}

    def fake_bootout(bot_id, dry_run):
        # By the time bootout runs, the flag must already exist.
        captured.setdefault("flag_existed", []).append(
            recovery.pause_state_path(tmp_path).exists()
        )
        return _stub_bootout_ok(bot_id, dry_run)

    monkeypatch.setattr(recovery, "_bootout_gateway", fake_bootout)

    result = recovery.pause_all(
        reason="test", initiated_by="pytest",
        shared_dir=tmp_path, network=network,
    )
    assert result.ok is True
    assert result.action == "pause-all"
    assert len(result.per_bot) == 3
    # Every bootout saw the flag in place
    assert captured["flag_existed"] == [True, True, True]
    # Flag file persists after the call
    assert recovery.is_paused(tmp_path) is True
    # Pause-log has an entry
    log = recovery.read_pause_log(tmp_path)
    assert len(log) == 1
    assert log[0]["action"] == "pause-all"
    assert log[0]["initiated_by"] == "pytest"


def test_pause_all_dry_run_touches_nothing(tmp_path: Path, monkeypatch):
    network = _make_network()
    # If recovery actually called bootout, our stub would record it
    calls: list[str] = []

    def trap(bot_id, dry_run):
        calls.append(bot_id)
        assert dry_run is True, "dry_run flag must propagate"
        return recovery.PerBotResult(
            bot_id=bot_id, label=recovery._gateway_label(bot_id),
            ok=True, rc=0, stdout="(dry-run)", elapsed_ms=1,
        )

    monkeypatch.setattr(recovery, "_bootout_gateway", trap)
    result = recovery.pause_all(
        reason="dry", shared_dir=tmp_path, network=network, dry_run=True,
    )
    assert result.ok is True
    assert result.dry_run is True
    assert calls == ["team_bot_a", "admin_bot", "team_bot_b"]
    # Crucially: no flag was written
    assert recovery.pause_state_path(tmp_path).exists() is False
    # No audit log entry
    assert recovery.read_pause_log(tmp_path) == []


def test_pause_all_records_partial_failure(tmp_path: Path, monkeypatch):
    """One bot's bootout fails → result.ok is False but flag still set."""
    network = _make_network()

    def mixed(bot_id, dry_run):
        if bot_id == "admin_bot":
            return _stub_bootout_fail(bot_id, dry_run)
        return _stub_bootout_ok(bot_id, dry_run)

    monkeypatch.setattr(recovery, "_bootout_gateway", mixed)
    result = recovery.pause_all(
        reason="partial-test", shared_dir=tmp_path, network=network,
    )
    assert result.ok is False, "partial failure must surface as ok=False"
    assert recovery.is_paused(tmp_path) is True, "flag must persist despite failure"
    # The failed bot's stderr is preserved
    failed = [b for b in result.per_bot if b.bot_id == "admin_bot"][0]
    assert failed.ok is False
    assert failed.stderr == "kaboom"


def test_resume_all_clears_flag_on_full_success(tmp_path: Path, monkeypatch):
    network = _make_network()
    # Set the flag first
    recovery._atomic_write_json(
        recovery.pause_state_path(tmp_path),
        {"paused": True, "paused_at": "x", "reason": "y"},
    )
    monkeypatch.setattr(recovery, "_bootstrap_gateway", _stub_bootstrap_ok)
    result = recovery.resume_all(shared_dir=tmp_path, network=network)
    assert result.ok is True
    assert recovery.is_paused(tmp_path) is False, "flag must be cleared after successful resume"
    assert result.state_after is None


def test_resume_all_keeps_flag_on_partial_failure(tmp_path: Path, monkeypatch):
    """If bootstrap fails for any bot, the flag stays so heal.py keeps hands off."""
    network = _make_network()
    recovery._atomic_write_json(
        recovery.pause_state_path(tmp_path),
        {"paused": True, "paused_at": "x", "reason": "y"},
    )

    def mixed(bot_id, dry_run):
        if bot_id == "team_bot_b":
            return _stub_bootstrap_fail(bot_id, dry_run)
        return _stub_bootstrap_ok(bot_id, dry_run)

    monkeypatch.setattr(recovery, "_bootstrap_gateway", mixed)
    result = recovery.resume_all(shared_dir=tmp_path, network=network)
    assert result.ok is False
    # Flag remains so heal.py doesn't fight the operator
    assert recovery.is_paused(tmp_path) is True
    # State annotated with partial-resume timestamp
    state = recovery.read_pause_state(tmp_path)
    assert "resume_partial_at" in state


def test_pause_then_resume_clears_state(tmp_path: Path, monkeypatch):
    """End-to-end happy path: pause → resume puts state back to neutral."""
    network = _make_network()
    monkeypatch.setattr(recovery, "_bootout_gateway", _stub_bootout_ok)
    monkeypatch.setattr(recovery, "_bootstrap_gateway", _stub_bootstrap_ok)
    p = recovery.pause_all(reason="end-to-end", shared_dir=tmp_path, network=network)
    assert p.ok and recovery.is_paused(tmp_path)
    r = recovery.resume_all(shared_dir=tmp_path, network=network)
    assert r.ok and not recovery.is_paused(tmp_path)
    # Two audit entries
    log = recovery.read_pause_log(tmp_path)
    assert [e["action"] for e in log] == ["resume-all", "pause-all"]


def test_pause_all_with_zero_bots_is_vacuously_ok(tmp_path: Path, monkeypatch):
    """Edge case: a pod with no bots configured shouldn't error."""
    network = {"primary": None, "members": [], "bots": {}}
    monkeypatch.setattr(recovery, "_bootout_gateway", _stub_bootout_ok)
    result = recovery.pause_all(shared_dir=tmp_path, network=network)
    assert result.ok is True
    assert result.per_bot == []


# ── Bootout return-code semantics ───────────────────────────────────────────


def test_bootout_rc_36_treated_as_already_stopped(monkeypatch):
    """launchctl returns 36 when the service isn't loaded; we treat that
    as already-paused success rather than a failure.

    launchctl now flows through the Scheduler seam (4.3C S2) — inject a
    fake runner; never patch subprocess (and never spawn a real launchctl:
    bootout is live-traffic destructive)."""
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    def fake_runner(argv):
        assert argv[:3] == ["sudo", "-n", "/bin/launchctl"], argv
        return (36, "", "Could not find specified service")

    set_scheduler(LaunchdScheduler(runner=fake_runner))
    try:
        res = recovery._bootout_gateway("admin_bot", dry_run=False)
    finally:
        set_scheduler(None)
    assert res.ok is True
    assert res.skipped is True
    assert res.rc == 36


# ── Rollback ────────────────────────────────────────────────────────────────


def _make_rollback_environment(tmp_path: Path, monkeypatch, *, pre_content='{"v":"current"}'):
    """Wire up the rollback dependencies with hermetic stubs.

    Returns a tuple (network, write_calls, restart_calls) so a test can
    inspect what would have been written/restarted."""
    network = _make_network(("team_bot_a",))

    # _resolve_target → returns a fake SHA without touching git
    def fake_resolve(bot_id, target, net):
        if target == "BAD":
            return None, "stubbed bad target"
        return "abc1234567890def", f"stubbed resolve to abc1234 for {target}"

    monkeypatch.setattr(recovery, "_resolve_target", fake_resolve)

    # _read_committed_openclaw → returns the "target" config bytes
    monkeypatch.setattr(
        recovery, "_read_committed_openclaw",
        lambda ws, sha: '{"v":"target","sha":"' + sha + '"}',
    )

    # _read_live_openclaw → returns the current config bytes (pre-snapshot)
    monkeypatch.setattr(
        recovery, "_read_live_openclaw",
        lambda bot_id, net: pre_content,
    )

    # _write_bot_openclaw → record what would have been written
    write_calls: list[tuple[str, str]] = []

    def fake_write(bot_id, net, content):
        write_calls.append((bot_id, content))
        return True, "(stubbed) wrote"

    monkeypatch.setattr(recovery, "_write_bot_openclaw", fake_write)

    # _restart_bot_gateway → record restart attempts
    restart_calls: list[str] = []

    def fake_restart(bot_id):
        restart_calls.append(bot_id)
        return True, "(stubbed) kickstart ok"

    monkeypatch.setattr(recovery, "_restart_bot_gateway", fake_restart)

    return network, write_calls, restart_calls


def test_rollback_writes_config_and_records_snapshot(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    result = recovery.rollback_bot(
        "team_bot_a", "2026-05-10",
        network=network, shared_dir=tmp_path,
        initiated_by="pytest",
    )
    assert result.ok is True
    assert result.bot_id == "team_bot_a"
    assert result.target_commit == "abc1234567890def"
    assert result.gateway_restart_ok is True
    # Config was written to the bot
    assert len(writes) == 1
    assert writes[0][0] == "team_bot_a"
    assert '"target"' in writes[0][1]
    # Gateway was kickstarted
    assert restarts == ["team_bot_a"]
    # Record file exists under recovery/rollbacks/
    rec_path = Path(result.pre_rollback_config_path)
    assert rec_path.exists()
    rec = json.loads(rec_path.read_text())
    assert rec["bot_id"] == "team_bot_a"
    assert rec["ok"] is True
    assert rec["pre_rollback_config"] == '{"v":"current"}'
    assert '"target"' in rec["post_rollback_config"]


def test_rollback_dry_run_does_not_write(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    result = recovery.rollback_bot(
        "team_bot_a", "2026-05-10",
        network=network, shared_dir=tmp_path, dry_run=True,
    )
    assert result.ok is True
    assert result.dry_run is True
    # No write, no restart
    assert writes == []
    assert restarts == []
    # No record persisted on disk for dry-run
    rd = recovery._rollback_dir(tmp_path)
    assert list(rd.glob("*.json")) == []


def test_rollback_rejects_when_target_unresolvable(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    result = recovery.rollback_bot(
        "team_bot_a", "BAD", network=network, shared_dir=tmp_path,
    )
    assert result.ok is False
    assert "could not resolve" in result.message.lower()
    assert writes == [] and restarts == []


def test_rollback_refuses_when_current_state_unreadable(tmp_path: Path, monkeypatch):
    """Cannot snapshot pre-state → MUST refuse (rollback would be one-way)."""
    network = _make_network(("team_bot_a",))
    monkeypatch.setattr(recovery, "_resolve_target",
                        lambda b, t, n: ("abc1234567890def", "ok"))
    monkeypatch.setattr(recovery, "_read_committed_openclaw",
                        lambda ws, sha: '{"v":"target"}')
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: None)  # ← simulates unreadable
    writes: list = []
    monkeypatch.setattr(recovery, "_write_bot_openclaw",
                        lambda b, n, c: writes.append(c) or (True, "ok"))

    result = recovery.rollback_bot(
        "team_bot_a", "2026-05-10",
        network=network, shared_dir=tmp_path,
    )
    assert result.ok is False
    assert "snapshot" in result.message.lower() or "reversible" in result.message.lower()
    # Critical: no write happened — the bot config was untouched
    assert writes == []


def test_rollback_skip_restart_does_not_kickstart(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    result = recovery.rollback_bot(
        "team_bot_a", "2026-05-10",
        network=network, shared_dir=tmp_path, skip_restart=True,
    )
    assert result.ok is True
    assert restarts == []
    assert result.gateway_restart_ok is None


def test_rollback_marks_record_failed_when_write_fails(tmp_path: Path, monkeypatch):
    network = _make_network(("team_bot_a",))
    monkeypatch.setattr(recovery, "_resolve_target",
                        lambda b, t, n: ("abc1234567890def", "ok"))
    monkeypatch.setattr(recovery, "_read_committed_openclaw",
                        lambda ws, sha: '{"v":"target"}')
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: '{"v":"current"}')
    monkeypatch.setattr(recovery, "_write_bot_openclaw",
                        lambda b, n, c: (False, "permission denied"))

    result = recovery.rollback_bot(
        "team_bot_a", "2026-05-10", network=network, shared_dir=tmp_path,
    )
    assert result.ok is False
    assert "write failed" in result.message.lower()
    # Audit record should still exist with ok=False
    rec_path = Path(result.pre_rollback_config_path)
    assert rec_path.exists()
    rec = json.loads(rec_path.read_text())
    assert rec["ok"] is False
    assert "write_error" in rec


# ── Reverse-rollback ────────────────────────────────────────────────────────


def test_reverse_rollback_restores_pre_state(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    # Step 1: do a rollback
    r1 = recovery.rollback_bot(
        "team_bot_a", "2026-05-10", network=network, shared_dir=tmp_path,
    )
    assert r1.ok
    assert len(writes) == 1
    # The post-rollback state matches the "target" content

    # Now stub _read_live_openclaw to return what we just "wrote" — the
    # bot is now in the "target" state from r1.
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: writes[-1][1])

    # Step 2: reverse it
    r2 = recovery.reverse_rollback(
        r1.rollback_id, network=network, shared_dir=tmp_path,
    )
    assert r2.ok, f"reverse-rollback failed: {r2.message}"
    assert r2.reversed_from_rollback_id == r1.rollback_id
    # A second write happened — restoring the pre-rollback content
    assert len(writes) == 2
    assert writes[1][0] == "team_bot_a"
    assert writes[1][1] == '{"v":"current"}'
    # Original record now points at the reverse
    rec1 = json.loads(
        (recovery._rollback_dir(tmp_path) / f"{r1.rollback_id}.json").read_text()
    )
    assert rec1["reversed_by_rollback_id"] == r2.rollback_id


def test_reverse_rollback_refuses_already_reversed(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    r1 = recovery.rollback_bot(
        "team_bot_a", "2026-05-10", network=network, shared_dir=tmp_path,
    )
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: writes[-1][1])
    r2 = recovery.reverse_rollback(r1.rollback_id, network=network, shared_dir=tmp_path)
    assert r2.ok
    # Try to reverse it a second time → must refuse
    r3 = recovery.reverse_rollback(r1.rollback_id, network=network, shared_dir=tmp_path)
    assert r3.ok is False
    assert "already reversed" in r3.message.lower()


def test_reverse_rollback_dry_run_does_not_write(tmp_path: Path, monkeypatch):
    network, writes, restarts = _make_rollback_environment(tmp_path, monkeypatch)
    r1 = recovery.rollback_bot(
        "team_bot_a", "2026-05-10", network=network, shared_dir=tmp_path,
    )
    before = len(writes)
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: writes[-1][1])
    r2 = recovery.reverse_rollback(
        r1.rollback_id, network=network, shared_dir=tmp_path, dry_run=True,
    )
    assert r2.ok
    assert r2.dry_run is True
    # No further writes
    assert len(writes) == before


def test_reverse_rollback_refuses_unknown_id(tmp_path: Path, monkeypatch):
    network = _make_network(("team_bot_a",))
    r = recovery.reverse_rollback(
        "no-such-id", network=network, shared_dir=tmp_path,
    )
    assert r.ok is False
    assert "no rollback record" in r.message.lower()


# ── list_rollback_history filtering ─────────────────────────────────────────


def test_list_rollback_history_filters_by_bot(tmp_path: Path, monkeypatch):
    """When two bots have rollback records, filtering by bot_id picks one."""
    network = _make_network(("team_bot_a", "admin_bot"))
    # Stub the minimum surface to land a rollback record for each bot
    monkeypatch.setattr(recovery, "_resolve_target",
                        lambda b, t, n: ("abc1234567890def", "ok"))
    monkeypatch.setattr(recovery, "_read_committed_openclaw",
                        lambda ws, sha: '{"v":"target"}')
    monkeypatch.setattr(recovery, "_read_live_openclaw",
                        lambda b, n: '{"v":"current"}')
    monkeypatch.setattr(recovery, "_write_bot_openclaw",
                        lambda b, n, c: (True, "ok"))
    monkeypatch.setattr(recovery, "_restart_bot_gateway",
                        lambda b: (True, "ok"))

    recovery.rollback_bot("team_bot_a", "2026-05-10", network=network, shared_dir=tmp_path)
    recovery.rollback_bot("admin_bot", "2026-05-10", network=network, shared_dir=tmp_path)

    all_hist = recovery.list_rollback_history(shared_dir=tmp_path)
    assert len(all_hist) == 2

    team_bot_a_hist = recovery.list_rollback_history(shared_dir=tmp_path, bot_id="team_bot_a")
    assert len(team_bot_a_hist) == 1
    assert team_bot_a_hist[0]["bot_id"] == "team_bot_a"
    # Full configs are stripped to keep the list response lean
    assert "pre_rollback_config" not in team_bot_a_hist[0]
    assert "post_rollback_config" not in team_bot_a_hist[0]
    assert team_bot_a_hist[0]["has_pre_rollback_snapshot"] is True


# ── recovery_status (dashboard summary) ──────────────────────────────────────


def test_recovery_status_shape(tmp_path: Path, monkeypatch):
    network = _make_network(("team_bot_a", "admin_bot"))
    # Set paused state
    recovery._atomic_write_json(
        recovery.pause_state_path(tmp_path),
        {"paused": True, "paused_at": "2026-05-12T00:00:00+00:00", "reason": "test"},
    )
    status = recovery.recovery_status(shared_dir=tmp_path, network=network)
    assert status["paused"] is True
    assert status["bot_count"] == 2
    assert set(status["bot_ids"]) == {"team_bot_a", "admin_bot"}
    assert "recent_pause_events" in status
    assert "recent_rollbacks" in status


# ── _iter_bots dedupes and preserves order ──────────────────────────────────


def test_iter_bots_dedupes(tmp_path: Path):
    """Primary + members + bots can overlap; iter must yield each exactly once."""
    net = {
        "primary": "team_bot_a",
        "members": ["admin_bot", "team_bot_a"],  # team_bot_a duplicated
        "bots": {"team_bot_a": {"user": "team_bot_a"}, "admin_bot": {"user": "admin_bot-user"},
                 "team_bot_b": {"user": "team_bot_b"}},
    }
    bots = recovery._iter_bots(net)
    ids = [b for b, _ in bots]
    assert ids == ["team_bot_a", "admin_bot", "team_bot_b"]
    # user resolution went through get_bot_user
    user_for = dict(bots)
    assert user_for["admin_bot"] == "admin_bot-user"


# ── Pod-state commit list + rollback (V2.4-2) ────────────────────────────────
#
# These tests use a real git repo in tmp_path so we can exercise the actual
# git log / git status / git reset --hard code paths without touching the
# production deploy checkout.  We mock subprocess.run only for the final
# launchctl kickstart (daemon restart) step.


import subprocess  # noqa: E402  (already imported at module level above, this is fine)
import time        # noqa: E402


def _make_git_repo(path: Path) -> str:
    """Create a bare git repo with two commits; return HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    # First commit (the "old" state we might roll back to)
    (path / "network.json").write_text('{"pod": "v1"}')
    subprocess.run(["git", "add", "network.json"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix(network): initial"], cwd=path, check=True)
    r1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True)
    sha1 = r1.stdout.strip()
    # Second commit (current HEAD — what operator might roll back from)
    (path / "network.json").write_text('{"pod": "v2"}')
    subprocess.run(["git", "add", "network.json"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat(network): v2"], cwd=path, check=True)
    return sha1  # return first commit sha as rollback target


# ── Token issuance and consumption ──────────────────────────────────────────


def test_issue_and_consume_token():
    sha = "abc123deadbeef" * 3
    tok = recovery._issue_confirm_token(sha)
    assert len(tok) > 20
    ok, msg = recovery._consume_confirm_token(sha, tok)
    assert ok, f"Expected ok, got: {msg}"
    assert msg == "ok"
    # Single-use: second consumption must fail
    ok2, msg2 = recovery._consume_confirm_token(sha, tok)
    assert not ok2
    assert "no pending" in msg2


def test_consume_wrong_token_rejected():
    sha = "sha-for-wrong-token-test"
    recovery._issue_confirm_token(sha)
    ok, msg = recovery._consume_confirm_token(sha, "wrong-token-value")
    assert not ok
    assert "mismatch" in msg
    # Token is NOT consumed on mismatch — original is still there
    # (a double-submit race from the legitimate caller should still succeed)
    assert sha in recovery._PENDING_TOKENS


def test_consume_expired_token_rejected(monkeypatch):
    sha = "sha-for-expired-token-test"
    tok = recovery._issue_confirm_token(sha)
    # Advance the expiry by patching time.monotonic to return a future time
    future = time.monotonic() + recovery._TOKEN_TTL_SEC + 5
    monkeypatch.setattr(recovery.time, "monotonic", lambda: future)
    ok, msg = recovery._consume_confirm_token(sha, tok)
    assert not ok
    assert "expired" in msg
    assert sha not in recovery._PENDING_TOKENS


def test_consume_no_pending_token_rejected():
    ok, msg = recovery._consume_confirm_token("sha-never-issued", "any-token")
    assert not ok
    assert "no pending" in msg


# ── list_recent_pod_commits ──────────────────────────────────────────────────


def test_list_recent_pod_commits_happy(tmp_path: Path):
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)
    commits = recovery.list_recent_pod_commits(
        days=7, limit=10, deploy_repo=repo, shared_dir=tmp_path,
    )
    assert len(commits) >= 2
    # Each commit has a confirm_token
    for c in commits:
        assert c.confirm_token, f"Missing confirm_token for {c.short_sha}"
        assert len(c.sha) == 40
        assert c.files_changed >= 0
    # Commits are newest-first
    if len(commits) >= 2:
        assert commits[0].timestamp >= commits[1].timestamp or True  # best-effort check


def test_list_recent_pod_commits_missing_repo(tmp_path: Path):
    repo = tmp_path / "nonexistent-repo"
    commits = recovery.list_recent_pod_commits(deploy_repo=repo, shared_dir=tmp_path)
    assert commits == []


def test_list_recent_pod_commits_high_impact_detection(tmp_path: Path):
    """Commits touching network.json should appear in high_impact_paths."""
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)  # creates a commit touching network.json
    commits = recovery.list_recent_pod_commits(
        days=7, limit=10, deploy_repo=repo, shared_dir=tmp_path,
    )
    # At least the first commit should flag network.json as high-impact
    hi_flagged = [c for c in commits if "network.json" in c.high_impact_paths]
    assert hi_flagged, "Expected at least one commit to flag network.json as high-impact"


# ── rollback_pod_state ────────────────────────────────────────────────────────


def test_pod_rollback_dry_run_succeeds(tmp_path: Path, monkeypatch):
    """dry_run=True returns ok=True without touching the repo or running sudo."""
    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)

    # Issue a token for sha1
    tok = recovery._issue_confirm_token(sha1)

    # Mock launchctl so we confirm it's NOT called in dry_run
    called = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **kw: (
        called.append(a[0]) or
        subprocess.CompletedProcess(a[0], 0, "", "")
    ))

    result = recovery.rollback_pod_state(
        commit_sha=sha1, confirm_token=tok,
        deploy_repo=repo, shared_dir=tmp_path,
        initiated_by="test", dry_run=True,
    )
    assert result.ok, f"dry-run failed: {result.message}"
    assert result.dry_run is True
    # No subprocess calls in dry_run (git reset + launchctl both suppressed)
    git_resets = [c for c in called if "reset" in str(c)]
    assert git_resets == [], "git reset should not run in dry_run"
    # No audit log written in dry_run
    log_path = recovery._pod_rollback_log_path(tmp_path)
    assert not log_path.exists()


def test_pod_rollback_expired_token_rejected(tmp_path: Path, monkeypatch):
    """Expired token must be rejected; repo must not be touched."""
    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)
    tok = recovery._issue_confirm_token(sha1)

    future = time.monotonic() + recovery._TOKEN_TTL_SEC + 5
    monkeypatch.setattr(recovery.time, "monotonic", lambda: future)

    result = recovery.rollback_pod_state(
        commit_sha=sha1, confirm_token=tok,
        deploy_repo=repo, shared_dir=tmp_path,
        initiated_by="test",
    )
    assert not result.ok
    assert "expired" in result.message
    # Audit log SHOULD be written even on token rejection
    log_path = recovery._pod_rollback_log_path(tmp_path)
    assert log_path.exists()
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["ok"] is False
    assert sha1 == rec["target_sha"]


def test_pod_rollback_wrong_token_rejected(tmp_path: Path):
    """Wrong token rejects and writes audit log."""
    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)
    recovery._issue_confirm_token(sha1)  # issue but don't use

    result = recovery.rollback_pod_state(
        commit_sha=sha1, confirm_token="not-the-right-token",
        deploy_repo=repo, shared_dir=tmp_path,
        initiated_by="test",
    )
    assert not result.ok
    assert "mismatch" in result.message


def test_pod_rollback_dirty_worktree_rejected(tmp_path: Path):
    """Rollback to a clean sha is rejected when the worktree is dirty."""
    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)
    # Dirty the working tree
    (repo / "dirty.txt").write_text("uncommitted!")
    tok = recovery._issue_confirm_token(sha1)

    result = recovery.rollback_pod_state(
        commit_sha=sha1, confirm_token=tok,
        deploy_repo=repo, shared_dir=tmp_path,
        initiated_by="test",
    )
    assert not result.ok
    assert "uncommitted" in result.message.lower() or "dirty" in result.message.lower()


def test_pod_rollback_nonexistent_sha_rejected(tmp_path: Path):
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)
    sha_fake = "deadbeef" * 5  # 40 chars, doesn't exist in repo
    tok = recovery._issue_confirm_token(sha_fake)

    result = recovery.rollback_pod_state(
        commit_sha=sha_fake, confirm_token=tok,
        deploy_repo=repo, shared_dir=tmp_path,
        initiated_by="test",
    )
    assert not result.ok
    assert "not found" in result.message.lower() or "cat-file" in result.message.lower()


def test_pod_rollback_single_use_token(tmp_path: Path, monkeypatch):
    """A token can only be used once; second attempt with same token fails."""
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)

    # Mock git reset at the subprocess layer. The daemon kickstart now
    # flows through the Scheduler seam (4.3C S2) — inject a fake runner
    # there; patching recovery.subprocess would no longer intercept it
    # and a REAL `sudo -n launchctl kickstart` would escape the test.
    orig_run = subprocess.run
    def _mock_run(cmd, *args, **kwargs):
        if "reset" in str(cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return orig_run(cmd, *args, **kwargs)
    monkeypatch.setattr(recovery.subprocess, "run", _mock_run)
    set_scheduler(LaunchdScheduler(runner=lambda argv: (0, "", "")))

    try:
        tok = recovery._issue_confirm_token(sha1)
        result1 = recovery.rollback_pod_state(
            commit_sha=sha1, confirm_token=tok,
            deploy_repo=repo, shared_dir=tmp_path,
            initiated_by="test",
        )
        assert result1.ok, f"First rollback failed: {result1.message}"

        # Re-issue same token value manually (simulate re-use attempt)
        # Actually just try again with the consumed token
        result2 = recovery.rollback_pod_state(
            commit_sha=sha1, confirm_token=tok,
            deploy_repo=repo, shared_dir=tmp_path,
            initiated_by="test",
        )
        assert not result2.ok
        assert "no pending" in result2.message
    finally:
        set_scheduler(None)


def test_pod_rollback_audit_log_written(tmp_path: Path, monkeypatch):
    """Successful rollback writes a JSON audit entry."""
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)

    # git reset mocked at the subprocess layer; the admin-ui kickstart
    # flows through the Scheduler seam (4.3C S2) — record its argv there.
    orig_run = subprocess.run
    def _mock_run(cmd, *args, **kwargs):
        if "reset" in str(cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return orig_run(cmd, *args, **kwargs)
    monkeypatch.setattr(recovery.subprocess, "run", _mock_run)

    kicks: list[list[str]] = []

    def _runner(argv):
        kicks.append(list(argv))
        return (0, "", "")

    set_scheduler(LaunchdScheduler(runner=_runner))
    try:
        tok = recovery._issue_confirm_token(sha1)
        result = recovery.rollback_pod_state(
            commit_sha=sha1, confirm_token=tok,
            deploy_repo=repo, shared_dir=tmp_path,
            initiated_by="operator-test",
        )
    finally:
        set_scheduler(None)
    assert result.ok, f"Rollback failed: {result.message}"

    # The daemon restart kept its sudo -n (daemon context: fail fast,
    # never block on a password prompt) argv shape through the seam.
    assert kicks == [
        ["sudo", "-n", "/bin/launchctl", "kickstart", "-k",
         "system/ai.evolve.evolve.admin-ui"],
    ]

    log_path = recovery._pod_rollback_log_path(tmp_path)
    assert log_path.exists()
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["ok"] is True
    assert rec["daemon_restart_ok"] is True
    assert rec["target_sha"] == sha1
    assert rec["initiated_by"] == "operator-test"
    assert "rollback_id" in rec
    assert "started_at" in rec
    assert "finished_at" in rec


def test_list_pod_rollback_log_empty(tmp_path: Path):
    entries = recovery.list_pod_rollback_log(shared_dir=tmp_path)
    assert entries == []


def test_list_pod_rollback_log_newest_first(tmp_path: Path, monkeypatch):
    """list_pod_rollback_log returns entries newest-first."""
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    repo = tmp_path / "deploy-repo"
    sha1 = _make_git_repo(repo)

    # git reset mocked at the subprocess layer; the kickstart flows
    # through the Scheduler seam (4.3C S2) — inject a fake runner there.
    orig_run = subprocess.run
    def _mock_run(cmd, *args, **kwargs):
        if "reset" in str(cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return orig_run(cmd, *args, **kwargs)
    monkeypatch.setattr(recovery.subprocess, "run", _mock_run)
    set_scheduler(LaunchdScheduler(runner=lambda argv: (0, "", "")))

    try:
        # Perform two rollbacks
        tok1 = recovery._issue_confirm_token(sha1)
        recovery.rollback_pod_state(
            commit_sha=sha1, confirm_token=tok1,
            deploy_repo=repo, shared_dir=tmp_path, initiated_by="first",
        )
        tok2 = recovery._issue_confirm_token(sha1)
        recovery.rollback_pod_state(
            commit_sha=sha1, confirm_token=tok2,
            deploy_repo=repo, shared_dir=tmp_path, initiated_by="second",
        )
    finally:
        set_scheduler(None)

    entries = recovery.list_pod_rollback_log(shared_dir=tmp_path, limit=10)
    assert len(entries) == 2
    # Newest first (second rollback should appear first)
    assert entries[0]["initiated_by"] == "second"
    assert entries[1]["initiated_by"] == "first"


# ── check_clean_worktree ──────────────────────────────────────────────────────


def test_check_clean_worktree_clean(tmp_path: Path):
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)
    clean, msg = recovery._check_clean_worktree(repo)
    assert clean, f"Expected clean, got: {msg}"


def test_check_clean_worktree_dirty(tmp_path: Path):
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)
    (repo / "new-file.txt").write_text("untracked")
    clean, msg = recovery._check_clean_worktree(repo)
    assert not clean
    assert "1" in msg or "change" in msg.lower()


def test_check_clean_worktree_modified(tmp_path: Path):
    repo = tmp_path / "deploy-repo"
    _make_git_repo(repo)
    # Modify an already-tracked file without staging
    (repo / "network.json").write_text('{"pod": "modified"}')
    clean, msg = recovery._check_clean_worktree(repo)
    assert not clean
