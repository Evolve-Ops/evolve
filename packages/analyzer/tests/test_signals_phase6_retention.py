"""tests/test_signals_phase6_retention.py — Phase 6 retention pruner."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import retention, store as signals_store  # noqa: E402


def _make_archived_signal(shared_dir: Path, *, age_days: int) -> Path:
    """Drop a fake archived signal file with mtime backdated by N days."""
    archived = signals_store.signals_root(shared_dir) / "archived"
    archived.mkdir(parents=True, exist_ok=True)
    path = archived / f"sig-{age_days}.json"
    path.write_text(
        json.dumps({
            "id": f"sig-{age_days}",
            "schema_version": 1,
            "signature": "test:t:scope",
            "producer": "test",
            "type": "t",
            "flavor": "activity",
            "severity": "info",
            "scope": "pod",
            "state": "resolved",
            "title": "test", "body": "", "details": {},
            "created_at": "2024-01-01T00:00:00+00:00",
            "last_observed_at": "2024-01-01T00:00:00+00:00",
            "observation_count": 1,
            "snoozed_until": None, "resolved_at": None,
            "state_history": [], "motivated_proposals": [], "deliveries": [],
        }),
        encoding="utf-8",
    )
    backdated = time.time() - age_days * 86400
    os.utime(path, (backdated, backdated))
    return path


def _make_log_file(shared_dir: Path, *, age_days: int) -> Path:
    log_dir = signals_store.signals_root(shared_dir) / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = log_dir / f"{file_date.isoformat()}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_retention_archives_older_than_window(tmp_path):
    keep = _make_archived_signal(tmp_path, age_days=30)
    drop = _make_archived_signal(tmp_path, age_days=120)

    result = retention.prune_retention(tmp_path, archived_days=90, log_days=365)
    assert result.archived_pruned == 1
    assert result.archived_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_retention_logs_older_than_window(tmp_path):
    keep = _make_log_file(tmp_path, age_days=30)
    drop = _make_log_file(tmp_path, age_days=400)

    result = retention.prune_retention(tmp_path, archived_days=90, log_days=365)
    assert result.log_files_pruned == 1
    assert result.log_files_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_retention_keeps_active_signals_untouched(tmp_path):
    """Firing + snoozed signals must never be pruned."""
    sig = signals_store.observe(
        tmp_path,
        signature="test:active:pod",
        producer="test", type="active",
        flavor="activity", severity="info", scope="pod",
        title="active",
    )
    retention.prune_retention(tmp_path, archived_days=0, log_days=0)
    located = signals_store.find_signal(tmp_path, sig.id)
    assert located is not None
    assert located[0].state == "firing"


def test_prune_retention_idempotent(tmp_path):
    _make_archived_signal(tmp_path, age_days=120)
    r1 = retention.prune_retention(tmp_path)
    r2 = retention.prune_retention(tmp_path)
    assert r1.archived_pruned == 1
    assert r2.archived_pruned == 0


def test_prune_retention_skips_unknown_log_filenames(tmp_path):
    """Files in log/ that don't look like YYYY-MM-DD.jsonl are kept
    (better safe than deleting unexpected data)."""
    log_dir = signals_store.signals_root(tmp_path) / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    weird = log_dir / "not-a-date.jsonl"
    weird.write_text("{}\n", encoding="utf-8")
    retention.prune_retention(tmp_path, log_days=1)
    assert weird.exists()


def test_prune_retention_no_signals_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.archived_pruned == 0
    assert result.log_files_pruned == 0


# ── Watchdog JSONL tests ──────────────────────────────────────────────────────

def _make_watchdog_file(shared_dir: Path, *, age_days: int) -> Path:
    wd_dir = shared_dir / "watchdog"
    wd_dir.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = wd_dir / f"{file_date.isoformat()}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_watchdog_older_than_window(tmp_path):
    keep = _make_watchdog_file(tmp_path, age_days=30)
    drop = _make_watchdog_file(tmp_path, age_days=400)

    result = retention.prune_retention(tmp_path, watchdog_days=365)
    assert result.watchdog_pruned == 1
    assert result.watchdog_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_watchdog_skips_unknown_filenames(tmp_path):
    wd_dir = tmp_path / "watchdog"
    wd_dir.mkdir()
    weird = wd_dir / "not-a-date.jsonl"
    weird.write_text("{}\n", encoding="utf-8")
    retention.prune_retention(tmp_path, watchdog_days=1)
    assert weird.exists()


def test_prune_watchdog_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.watchdog_pruned == 0
    assert result.watchdog_kept == 0


# ── Proposals archived/ tests ─────────────────────────────────────────────────

def _make_archived_proposal(shared_dir: Path, *, age_days: int) -> Path:
    proposals_dir = shared_dir / "proposals" / "archived"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    path = proposals_dir / f"proposal-{age_days}.json"
    path.write_text(json.dumps({"id": f"proposal-{age_days}", "status": "rejected"}))
    backdated = time.time() - age_days * 86400
    os.utime(path, (backdated, backdated))
    return path


def test_prune_proposals_older_than_window(tmp_path):
    keep = _make_archived_proposal(tmp_path, age_days=30)
    drop = _make_archived_proposal(tmp_path, age_days=120)

    result = retention.prune_retention(tmp_path, proposals_days=90)
    assert result.proposals_pruned == 1
    assert result.proposals_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_proposals_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.proposals_pruned == 0
    assert result.proposals_kept == 0


# ── Alerts log retention (2026-06-01) ───────────────────────────────────────


def _make_alerts_log(shared_dir: Path, stream: str, *, age_days: int) -> Path:
    """Create a date-partitioned alerts log file dated N days ago."""
    log_dir = shared_dir / "alerts" / stream
    log_dir.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = log_dir / f"{file_date.isoformat()}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_alerts_logs_older_than_window(tmp_path):
    """Dispatcher / suppressed / failures logs older than the alerts
    cutoff get pruned. Anchored to the 2026-06-01 disk-fillup incident."""
    keep_disp = _make_alerts_log(tmp_path, "dispatcher", age_days=10)
    drop_disp = _make_alerts_log(tmp_path, "dispatcher", age_days=45)
    keep_sup = _make_alerts_log(tmp_path, "dispatcher-suppressed", age_days=10)
    drop_sup = _make_alerts_log(tmp_path, "dispatcher-suppressed", age_days=45)
    keep_fail = _make_alerts_log(tmp_path, "delivery-failures", age_days=10)
    drop_fail = _make_alerts_log(tmp_path, "delivery-failures", age_days=45)
    keep_dl = _make_alerts_log(tmp_path, "dead-letter", age_days=10)
    drop_dl = _make_alerts_log(tmp_path, "dead-letter", age_days=45)
    # dispatcher-queued (2026-08-25 lane split) — it carries what used to be
    # the BULK of the dispatcher stream's volume, so an unswept queued lane
    # would re-create the 2026-06-01 disk-fillup under a new filename.
    keep_q = _make_alerts_log(tmp_path, "dispatcher-queued", age_days=10)
    drop_q = _make_alerts_log(tmp_path, "dispatcher-queued", age_days=45)

    result = retention.prune_retention(tmp_path, alerts_days=30)
    assert result.alerts_pruned == 5
    assert result.alerts_kept == 5
    assert keep_disp.exists() and not drop_disp.exists()
    assert keep_sup.exists() and not drop_sup.exists()
    assert keep_fail.exists() and not drop_fail.exists()
    assert keep_dl.exists() and not drop_dl.exists()
    assert keep_q.exists() and not drop_q.exists()


def test_prune_alerts_logs_no_dir_is_noop(tmp_path):
    """A pod with no alerts dir at all (fresh install) sweeps cleanly."""
    result = retention.prune_retention(tmp_path)
    assert result.alerts_pruned == 0
    assert result.alerts_kept == 0


def test_prune_alerts_logs_keeps_non_date_filenames(tmp_path):
    """The legacy flat ``alerts/dispatcher.jsonl`` (pre-rotation) lives
    next to the date-partition dir during the transition. The retention
    sweep targets ``alerts/<stream>/<YYYY-MM-DD>.jsonl`` files only —
    it must not delete the legacy flat file or unparseable filenames."""
    (tmp_path / "alerts").mkdir()
    flat = tmp_path / "alerts" / "dispatcher.jsonl"
    flat.write_text("{}\n", encoding="utf-8")

    log_dir = tmp_path / "alerts" / "dispatcher"
    log_dir.mkdir()
    weird = log_dir / "not-a-date.jsonl"
    weird.write_text("{}\n", encoding="utf-8")

    result = retention.prune_retention(tmp_path, alerts_days=30)
    assert result.alerts_pruned == 0
    assert flat.exists()
    assert weird.exists()


# ── Incidents day-dir tests ───────────────────────────────────────────────────

def _make_incident_dir(shared_dir: Path, *, age_days: int) -> Path:
    inc_dir = shared_dir / "incidents"
    inc_dir.mkdir(parents=True, exist_ok=True)
    dir_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    day_dir = inc_dir / dir_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "team_bot_a-120000000000-gateway_down.json").write_text(
        json.dumps({"bot_id": "team_bot_a", "type": "gateway_down"})
    )
    return day_dir


def test_prune_incidents_older_than_window(tmp_path):
    keep = _make_incident_dir(tmp_path, age_days=10)
    drop = _make_incident_dir(tmp_path, age_days=60)

    result = retention.prune_retention(tmp_path, incidents_days=30)
    assert result.incidents_dirs_pruned == 1
    assert result.incidents_dirs_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_incidents_skips_unknown_dir_names(tmp_path):
    inc_dir = tmp_path / "incidents"
    inc_dir.mkdir()
    weird = inc_dir / "not-a-date"
    weird.mkdir()
    (weird / "something.json").write_text("{}")

    retention.prune_retention(tmp_path, incidents_days=1)
    assert weird.exists()


def test_prune_incidents_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.incidents_dirs_pruned == 0
    assert result.incidents_dirs_kept == 0


def test_prune_incidents_idempotent(tmp_path):
    _make_incident_dir(tmp_path, age_days=60)
    r1 = retention.prune_retention(tmp_path, incidents_days=30)
    r2 = retention.prune_retention(tmp_path, incidents_days=30)
    assert r1.incidents_dirs_pruned == 1
    assert r2.incidents_dirs_pruned == 0


def test_prune_incidents_keeps_today(tmp_path):
    """Today's dir must never be pruned — heal.py is actively writing to it."""
    today = _make_incident_dir(tmp_path, age_days=0)
    result = retention.prune_retention(tmp_path, incidents_days=30)
    assert result.incidents_dirs_pruned == 0
    assert today.exists()


# ── delivery_monitor/ledger/<YYYY-MM-DD>.jsonl (90 days) ────────────────────
# Spec: internal/spec-proactive-delivery-monitor-2026-06-10.md §6.5. The
# ledger is U0's confirmed-deliveries source — pruning must be filename-
# date based (same shape as the signals log roll).


def _make_delivery_ledger(shared_dir: Path, *, age_days: int) -> Path:
    ledger_dir = shared_dir / "delivery_monitor" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = ledger_dir / f"{file_date.isoformat()}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_delivery_ledger_older_than_window(tmp_path):
    keep = _make_delivery_ledger(tmp_path, age_days=30)
    drop = _make_delivery_ledger(tmp_path, age_days=120)

    result = retention.prune_retention(tmp_path, delivery_ledger_days=90)
    assert result.delivery_ledger_pruned == 1
    assert result.delivery_ledger_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_delivery_ledger_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.delivery_ledger_pruned == 0
    assert result.delivery_ledger_kept == 0


def test_prune_delivery_ledger_keeps_non_date_filenames(tmp_path):
    ledger_dir = tmp_path / "delivery_monitor" / "ledger"
    ledger_dir.mkdir(parents=True)
    odd = ledger_dir / "notes.jsonl"
    odd.write_text("{}\n", encoding="utf-8")
    result = retention.prune_retention(tmp_path, delivery_ledger_days=90)
    assert result.delivery_ledger_pruned == 0
    assert result.delivery_ledger_kept == 1
    assert odd.exists()


# ── breakers/runner-log/<date>.jsonl (footprint F-5-B retention floor) ────────
# Date-based, same shape as the delivery ledger / signals log roll.


def _make_runner_log(shared_dir: Path, *, age_days: int) -> Path:
    log_dir = shared_dir / "breakers" / "runner-log"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_date = (datetime.now(timezone.utc) - timedelta(days=age_days)).date()
    path = log_dir / f"{file_date.isoformat()}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_runner_log_older_than_window(tmp_path):
    keep = _make_runner_log(tmp_path, age_days=7)
    drop = _make_runner_log(tmp_path, age_days=30)

    result = retention.prune_retention(tmp_path, runner_log_days=14)
    assert result.runner_log_pruned == 1
    assert result.runner_log_kept == 1
    assert keep.exists()
    assert not drop.exists()


def test_prune_runner_log_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.runner_log_pruned == 0
    assert result.runner_log_kept == 0


def test_prune_runner_log_keeps_non_date_filenames(tmp_path):
    log_dir = tmp_path / "breakers" / "runner-log"
    log_dir.mkdir(parents=True)
    odd = log_dir / "notes.jsonl"
    odd.write_text("{}\n", encoding="utf-8")
    result = retention.prune_retention(tmp_path, runner_log_days=14)
    assert result.runner_log_pruned == 0
    assert result.runner_log_kept == 1
    assert odd.exists()


def test_prune_runner_log_spares_last_decision_sidecar(tmp_path):
    # The .last-decision.json change-detection sidecar is a dotfile (not a
    # date-stamped *.jsonl) and must never be swept, even when ancient.
    drop = _make_runner_log(tmp_path, age_days=99)
    sidecar = tmp_path / "breakers" / "runner-log" / ".last-decision.json"
    sidecar.write_text("{}", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=99)).timestamp()
    os.utime(sidecar, (old, old))

    result = retention.prune_retention(tmp_path, runner_log_days=14)
    assert result.runner_log_pruned == 1
    assert not drop.exists()
    assert sidecar.exists()


# ── archived-bots/<bot>-<YYYY-MM-DD>[-<n>]/ (180 days) ──────────────────────
# evolve_admin.retire writes one dir per retirement; before this category
# there was no prune path and it grew forever (1.4 GB observed 2026-06-28).
# Age is parsed from the directory name (the retirement date), same shape as
# the incidents day-dir prune.


def _make_archived_bot(shared_dir: Path, name: str) -> Path:
    """Create a fake archived-bot dir with a couple of files inside."""
    d = shared_dir / "archived-bots" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "STATUS.json").write_text("{}", encoding="utf-8")
    (d / "openclaw").mkdir(exist_ok=True)
    (d / "openclaw" / "log.txt").write_text("x", encoding="utf-8")
    return d


def test_prune_archived_bots_older_than_window(tmp_path):
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).date().isoformat()
    new_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    drop = _make_archived_bot(tmp_path, f"nova-{old_date}")
    keep = _make_archived_bot(tmp_path, f"vela-{new_date}")

    result = retention.prune_retention(tmp_path, archived_bots_days=180)
    assert result.archived_bots_pruned == 1
    assert result.archived_bots_kept == 1
    assert not drop.exists()
    assert keep.exists()


def test_prune_archived_bots_handles_collision_suffix(tmp_path):
    """``nova-<date>-2`` (same-day re-archive collision suffix) must still
    parse its date and prune."""
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).date().isoformat()
    drop = _make_archived_bot(tmp_path, f"nova-{old_date}-2")
    result = retention.prune_retention(tmp_path, archived_bots_days=180)
    assert result.archived_bots_pruned == 1
    assert not drop.exists()


def test_prune_archived_bots_keeps_non_date_names(tmp_path):
    """A directory whose name has no parseable retirement date is never
    deleted, no matter how old."""
    odd = tmp_path / "archived-bots" / "README"
    odd.mkdir(parents=True)
    (odd / "x").write_text("y", encoding="utf-8")
    result = retention.prune_retention(tmp_path, archived_bots_days=1)
    assert result.archived_bots_pruned == 0
    assert result.archived_bots_kept == 1
    assert odd.exists()


def test_prune_archived_bots_no_dir_is_noop(tmp_path):
    result = retention.prune_retention(tmp_path)
    assert result.archived_bots_pruned == 0
    assert result.archived_bots_kept == 0


def test_prune_archived_bots_idempotent(tmp_path):
    old_date = (datetime.now(timezone.utc) - timedelta(days=200)).date().isoformat()
    _make_archived_bot(tmp_path, f"nova-{old_date}")
    r1 = retention.prune_retention(tmp_path, archived_bots_days=180)
    r2 = retention.prune_retention(tmp_path, archived_bots_days=180)
    assert r1.archived_bots_pruned == 1
    assert r2.archived_bots_pruned == 0


def test_remove_archived_bot_dir_refuses_outside_root(tmp_path):
    """Containment guard: a path that is not a direct child of the
    archived-bots root is never deleted."""
    archive_root = tmp_path / "archived-bots"
    archive_root.mkdir()
    outsider = tmp_path / "elsewhere"
    outsider.mkdir()
    (outsider / "keep").write_text("z", encoding="utf-8")
    assert retention._remove_archived_bot_dir(outsider, archive_root) is False
    assert outsider.exists()
