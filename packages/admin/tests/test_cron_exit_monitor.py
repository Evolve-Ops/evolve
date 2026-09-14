"""tests/test_cron_exit_monitor.py — loaded-but-failing cron detection.

PR #2669 made oc-log-rotate exit non-zero when a truncation fails; this
producer is the missing consumer of that exit code. pod_health's
_check_launchd covers *not loaded* — these tests pin the *loaded but
failing* gap:

  - a managed calendar job with non-zero LastExitStatus fires ONE
    cron_job_failed Signal (per-label signature, producer-default
    severity, platform category)
  - the raw wait(2) status is decoded (256 → "exit code 1", 15 →
    "terminated by signal 15")
  - a subsequent clean run sweep-resolves the Signal
  - KeepAlive daemons and StartInterval jobs are out of scope
  - per-bot jobs scope to the bot; evolve-user jobs scope to the pod
  - tri-state per PR #1579: sudo escalation failure (status_error =
    "cannot_escalate" from the Scheduler seam) emits ONE meta-Signal,
    leaves existing per-label Signals firing (no sweep on unknown
    state), and the meta-Signal resolves once the probe works again;
    a transient per-label probe error protects that label from the
    sweep without emitting anything
  - a not-loaded job (managed=False) is ignored — that finding belongs
    to pod_health._check_launchd
  - the hourly audit-scheduler tick runs the sweep (Phase 0c) and
    records counters / errors
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.cron_exit_monitor import (  # noqa: E402
    PRODUCER,
    TYPE_CANNOT_ESCALATE,
    TYPE_FAILED,
    emit_cron_exit_signals,
    list_managed_cron_jobs,
)

ROTATE_LABEL = "ai.evolve.evolve.oc-log-rotate"
BACKUP_LABEL = "ai.evolve.team_bot_a.backup"


def _make_shared(tmp_path: Path) -> Path:
    shared = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True)
    return shared


def _network(shared: Path) -> dict:
    return {"networkId": "test", "members": ["team_bot_a"], "sharedDir": str(shared)}


def _write_plist(
    plist_dir: Path,
    label: str,
    *,
    user: str = "evolve",
    calendar: dict | None = None,
    interval: int | None = None,
    keepalive: bool = False,
    stderr_path: str | None = None,
) -> None:
    data: dict = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/true"],
        "UserName": user,
    }
    if calendar is not None:
        data["StartCalendarInterval"] = calendar
    if interval is not None:
        data["StartInterval"] = interval
    if keepalive:
        data["KeepAlive"] = True
    if stderr_path:
        data["StandardErrorPath"] = stderr_path
    plist_dir.mkdir(parents=True, exist_ok=True)
    with (plist_dir / f"{label}.plist").open("wb") as f:
        plistlib.dump(data, f)


def _st(
    last_exit: str | None = "0",
    *,
    managed: bool = True,
    status_error: str | None = None,
) -> dict:
    """A Scheduler ``status()`` dict (the seam shape)."""
    return {
        "installed": True,
        "managed": managed,
        "running": False,
        "pid": None,
        "last_exit": last_exit if managed else None,
        "status_error": status_error,
    }


def _probe(responses: dict[str, dict]):
    """Fake status_fn: per-label status dict with a clean default for
    labels the test doesn't care about."""
    def fn(label: str) -> dict:
        return responses.get(label, _st("0"))
    return fn


_CANNOT_ESCALATE = _st(None, managed=False, status_error="cannot_escalate")


def _firing(shared: Path) -> list:
    import signals.store as signals_store
    return [
        s for s in signals_store.iter_signals(shared, subdirs=("firing",))
        if s.producer == PRODUCER
    ]


def _archived(shared: Path) -> list:
    import signals.store as signals_store
    return [
        s for s in signals_store.iter_signals(shared, subdirs=("archived",))
        if s.producer == PRODUCER
    ]


# ── enumeration ──────────────────────────────────────────────────────────────

def test_enumeration_skips_keepalive_interval_and_foreign_plists(tmp_path):
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})
    _write_plist(plist_dir, "ai.evolve.evolve.admin-ui", keepalive=True)
    _write_plist(plist_dir, "ai.evolve.evolve.pod-health", interval=60)
    _write_plist(plist_dir, "com.apple.something", calendar={"Hour": 1, "Minute": 0})

    jobs = list_managed_cron_jobs(plist_dir)
    assert [j.label for j in jobs] == [ROTATE_LABEL]


def test_enumeration_of_missing_dir_is_empty(tmp_path):
    assert list_managed_cron_jobs(tmp_path / "nope") == []


# ── firing + decode ──────────────────────────────────────────────────────────

def test_failing_cron_fires_pod_scoped_signal(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30},
                 stderr_path="/Users/evolve/.openclaw/logs/evolve-oc-log-rotate.err.log")

    # wait(2) status 256 == exit code 1 (the #2669 failure shape).
    summary = emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({ROTATE_LABEL: _st("256")}),
    )

    assert summary == {"checked": 1, "failing": 1, "cannot_escalate": False}
    firing = _firing(shared)
    assert len(firing) == 1
    sig = firing[0]
    assert sig.type == TYPE_FAILED
    assert sig.signature == f"{PRODUCER}:{TYPE_FAILED}:{ROTATE_LABEL}"
    assert sig.scope == "pod"
    assert sig.bot_id is None
    assert sig.severity == "warn"        # producer_severity registration
    assert sig.category == "platform"    # PRODUCER_CATEGORY_DEFAULT registration
    assert "exit code 1" in sig.body
    assert sig.details["last_exit_status"] == 256
    assert "evolve-oc-log-rotate.err.log" in sig.details["fix_steps"]


def test_signal_kill_is_decoded(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})

    emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({ROTATE_LABEL: _st("15")}),
    )

    assert "terminated by signal 15" in _firing(shared)[0].body


def test_per_bot_job_scopes_to_bot(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, BACKUP_LABEL, user="team_bot_a",
                 calendar={"Hour": 2, "Minute": 0})

    emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({BACKUP_LABEL: _st("256")}),
    )

    sig = _firing(shared)[0]
    assert sig.scope == "bot"
    assert sig.bot_id == "team_bot_a"


def test_clean_and_not_loaded_jobs_fire_nothing(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})
    _write_plist(plist_dir, BACKUP_LABEL, user="team_bot_a",
                 calendar={"Hour": 2, "Minute": 0})

    summary = emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({
            ROTATE_LABEL: _st("0"),
            # Not loaded: pod_health._check_launchd owns this finding.
            BACKUP_LABEL: _st(None, managed=False),
        }),
    )

    assert summary["failing"] == 0
    assert _firing(shared) == []


# ── recovery sweep ───────────────────────────────────────────────────────────

def test_cleared_exit_status_sweeps_signal_resolved(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})

    emit_cron_exit_signals(shared, _network(shared), plist_dir=plist_dir,
                           status_fn=_probe({ROTATE_LABEL: _st("256")}))
    fire_id = _firing(shared)[0].id

    emit_cron_exit_signals(shared, _network(shared), plist_dir=plist_dir,
                           status_fn=_probe({}))

    assert _firing(shared) == []
    resolved = [s for s in _archived(shared) if s.id == fire_id]
    assert len(resolved) == 1
    assert resolved[0].state == "resolved"


# ── tri-state (PR #1579 convention) ──────────────────────────────────────────

def test_cannot_escalate_emits_single_meta_signal_and_skips_sweep(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})
    _write_plist(plist_dir, BACKUP_LABEL, user="team_bot_a",
                 calendar={"Hour": 2, "Minute": 0})

    # Seed a firing per-label Signal from a previous (working) run.
    emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({BACKUP_LABEL: _st("256")}),
    )
    assert len(_firing(shared)) == 1

    summary = emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=lambda label: dict(_CANNOT_ESCALATE),
    )

    assert summary["cannot_escalate"] is True
    firing = _firing(shared)
    metas = [s for s in firing if s.type == TYPE_CANNOT_ESCALATE]
    assert len(metas) == 1, "exactly one meta-signal, never a per-daemon cascade"
    # The pre-existing per-label Signal must survive: its state is unknown,
    # so the tooling failure must not auto-resolve it.
    per_label = [s for s in firing if s.type == TYPE_FAILED]
    assert len(per_label) == 1
    assert per_label[0].signature == f"{PRODUCER}:{TYPE_FAILED}:{BACKUP_LABEL}"


def test_transient_probe_error_protects_label_from_sweep(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})

    emit_cron_exit_signals(shared, _network(shared), plist_dir=plist_dir,
                           status_fn=_probe({ROTATE_LABEL: _st("256")}))
    assert len(_firing(shared)) == 1

    # Non-escalation probe failure (e.g. timeout): no meta-signal, no new
    # observation — but the firing Signal must NOT be auto-resolved.
    summary = emit_cron_exit_signals(
        shared, _network(shared), plist_dir=plist_dir,
        status_fn=_probe({ROTATE_LABEL: _st(None, managed=False,
                                            status_error="error")}),
    )

    assert summary == {"checked": 0, "failing": 0, "cannot_escalate": False}
    firing = _firing(shared)
    assert len(firing) == 1
    assert firing[0].type == TYPE_FAILED


def test_meta_signal_resolves_when_probe_recovers(tmp_path):
    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})

    emit_cron_exit_signals(shared, _network(shared), plist_dir=plist_dir,
                           status_fn=lambda label: dict(_CANNOT_ESCALATE))
    metas = [s for s in _firing(shared) if s.type == TYPE_CANNOT_ESCALATE]
    assert len(metas) == 1
    meta_id = metas[0].id

    emit_cron_exit_signals(shared, _network(shared), plist_dir=plist_dir,
                           status_fn=_probe({}))

    assert _firing(shared) == []
    assert any(s.id == meta_id and s.state == "resolved" for s in _archived(shared))


# ── audit-scheduler tick integration (Phase 0c) ──────────────────────────────

def _quiet_tick_phases(monkeypatch):
    """Stub the infra-audit + outbox phases — these tests pin Phase 0c only."""
    from evolve_admin.applications import audit_scheduler
    monkeypatch.setattr(audit_scheduler, "_run_infra_audit_if_due",
                        lambda shared_dir, network, result: None)
    monkeypatch.setattr(audit_scheduler, "_drain_audit_outboxes",
                        lambda shared_dir, network, result: None)
    return audit_scheduler


def test_tick_runs_cron_exit_sweep(tmp_path, monkeypatch):
    audit_scheduler = _quiet_tick_phases(monkeypatch)
    from evolve_admin import cron_exit_monitor

    shared = _make_shared(tmp_path)
    plist_dir = tmp_path / "daemons"
    _write_plist(plist_dir, ROTATE_LABEL, calendar={"Hour": 4, "Minute": 30})
    monkeypatch.setattr(cron_exit_monitor, "_default_plist_dir", lambda: plist_dir)
    monkeypatch.setattr(cron_exit_monitor, "_status_probe",
                        _probe({ROTATE_LABEL: _st("256")}))

    result = audit_scheduler.tick(shared, _network(shared))

    assert result.cron_jobs_checked == 1
    assert result.cron_jobs_failing == 1
    assert result.as_dict()["cron_jobs_failing"] == 1
    assert len(_firing(shared)) == 1


def test_tick_records_cron_exit_errors_without_crashing(tmp_path, monkeypatch):
    audit_scheduler = _quiet_tick_phases(monkeypatch)
    from evolve_admin import cron_exit_monitor

    shared = _make_shared(tmp_path)

    def _boom(shared_dir, network=None, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cron_exit_monitor, "emit_cron_exit_signals", _boom)

    result = audit_scheduler.tick(shared, _network(shared))

    assert any(e["stage"] == "cron_exit_monitor" for e in result.errors)
    assert result.cron_jobs_failing == 0
