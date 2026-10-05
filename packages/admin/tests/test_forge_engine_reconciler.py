"""Tests for the post-install cost reconciler (2026-06-03).

After a forge install completes, _reconcile_install_cost sums the actual
cost of forge-tagged turns within the job's time window, writes it back
onto the ForgeJob, and emits a forge_install_cost_overrun Signal when
the actual exceeded the projected high-band ceiling for an operator-
confirmed install.

These tests pin the contract end-to-end using on-disk forge_session
annotations + a monkeypatched usage_analytics.load_turns so they don't
depend on a live OC environment.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set up a ForgeJob in the completed dir, a forge_session window for
    its bot/date, and a mock usage_analytics.load_turns that returns the
    turns the test passes via the `turns` mutable list.
    """
    from evolve_admin.applications import forge_engine, forge_jobs
    import forge_sessions as fs
    import usage_analytics as ua

    bot_id = "team_bot_c"
    app_id = "unified-tasks"
    job = forge_jobs.create_install_job(
        pkg_id="p-test",
        app_id=app_id,
        bot_id=bot_id,
        gallery_version="wizard",
        shared_dir=tmp_path,
        operator_confirmed=True,
        projected_cost_mid_usd=10.0,
        projected_cost_high_usd=20.0,
    )
    # Move job into completed dir so reconciler updates the right file
    _job = forge_jobs.load_job(job.job_id, tmp_path)
    # Force a known created/completed window so we can place turns in it
    _job.created_at = "2026-06-03T10:00:00Z"
    _job.last_updated = "2026-06-03T10:30:00Z"
    forge_jobs._completed_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    forge_jobs._atomic_write(
        forge_jobs._completed_path(_job.job_id, tmp_path),
        forge_jobs._job_to_dict(_job),
    )
    active_path = forge_jobs._active_path(_job.job_id, tmp_path)
    if active_path.exists():
        active_path.unlink()

    # Write a forge_session window matching the job for the bot
    fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id=bot_id,
        job_id=_job.job_id,
        suffix="",
        kind="build",
        start_ts=datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc),
        timeout_sec=1800,  # 30 min
        trigger_subkind="operator_confirmed_install",
    )

    # Stub usage_analytics.load_turns: tests mutate the turns list
    turns_holder: list[dict] = []
    monkeypatch.setattr(ua, "load_turns", lambda *a, **kw: list(turns_holder))

    # Capture signals_store.observe calls
    observed: list[dict] = []
    from signals import store as ss

    def _fake_observe(shared_dir, **kw):
        observed.append({"shared_dir": shared_dir, **kw})
        return None
    monkeypatch.setattr(ss, "observe", _fake_observe)

    return {
        "tmp_path": tmp_path,
        "bot_id": bot_id,
        "app_id": app_id,
        "job": forge_jobs.load_job(_job.job_id, tmp_path),
        "turns": turns_holder,
        "observed": observed,
        "forge_engine": forge_engine,
        "forge_jobs": forge_jobs,
    }


def _turn(ts: str, cost: float) -> dict:
    return {"ts": ts, "cost": cost, "source": "forge", "channel": "unknown"}


def test_reconciler_sums_cost_and_writes_to_job(env):
    env["turns"].extend([
        _turn("2026-06-03T10:05:00Z", 3.0),
        _turn("2026-06-03T10:15:00Z", 5.5),
        # Outside the window — must NOT count
        _turn("2026-06-03T11:00:00Z", 99.0),
    ])
    env["forge_engine"]._reconcile_install_cost(env["job"], env["tmp_path"])

    reloaded = env["forge_jobs"].load_job(env["job"].job_id, env["tmp_path"])
    assert reloaded.actual_cost_usd == 8.5


def test_reconciler_emits_overrun_signal_when_over_high_band(env):
    # Projected high = $20; actual = $30 → overrun
    env["turns"].extend([
        _turn("2026-06-03T10:05:00Z", 15.0),
        _turn("2026-06-03T10:20:00Z", 15.0),
    ])
    env["forge_engine"]._reconcile_install_cost(env["job"], env["tmp_path"])

    overruns = [s for s in env["observed"]
                if s.get("type") == "forge_install_cost_overrun"]
    assert len(overruns) == 1
    sig = overruns[0]
    assert sig["bot_id"] == env["bot_id"]
    assert sig["details"]["actual_usd"] == 30.0
    assert sig["details"]["projected_high_usd"] == 20.0
    assert sig["severity"] == "warn"


def test_reconciler_does_not_signal_when_under_high_band(env):
    # Projected high = $20; actual = $15 → no overrun
    env["turns"].extend([_turn("2026-06-03T10:05:00Z", 15.0)])
    env["forge_engine"]._reconcile_install_cost(env["job"], env["tmp_path"])

    overruns = [s for s in env["observed"]
                if s.get("type") == "forge_install_cost_overrun"]
    assert overruns == []


def test_reconciler_does_not_signal_when_not_operator_confirmed(env, monkeypatch):
    # Flip the job's operator_confirmed flag and re-save
    env["job"].operator_confirmed = False
    env["forge_jobs"]._atomic_write(
        env["forge_jobs"]._completed_path(env["job"].job_id, env["tmp_path"]),
        env["forge_jobs"]._job_to_dict(env["job"]),
    )
    reloaded = env["forge_jobs"].load_job(env["job"].job_id, env["tmp_path"])

    env["turns"].extend([_turn("2026-06-03T10:05:00Z", 100.0)])
    env["forge_engine"]._reconcile_install_cost(reloaded, env["tmp_path"])

    overruns = [s for s in env["observed"]
                if s.get("type") == "forge_install_cost_overrun"]
    assert overruns == []


def test_reconciler_no_op_when_no_windows(env):
    """A job with no forge_session window (e.g. forge subprocess never
    fired) still reconciles cleanly — no exception, no signal."""
    # Delete the annotation file
    ann = env["tmp_path"] / "forge_sessions" / env["bot_id"] / "2026-06-03.jsonl"
    if ann.exists():
        ann.unlink()

    env["turns"].extend([_turn("2026-06-03T10:05:00Z", 50.0)])
    env["forge_engine"]._reconcile_install_cost(env["job"], env["tmp_path"])

    # actual_cost_usd is unchanged (None) since no windows means no sum
    reloaded = env["forge_jobs"].load_job(env["job"].job_id, env["tmp_path"])
    assert reloaded.actual_cost_usd is None
    assert env["observed"] == []
