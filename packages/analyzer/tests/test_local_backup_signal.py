"""tests/test_local_backup_signal.py — Time Machine health monitor."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import local_backup_signal as lbs  # noqa: E402
from local_backup import LocalBackupStatus, TMDestination  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _status(**overrides) -> LocalBackupStatus:
    base = dict(
        available=True,
        configured=True,
        destinations=[TMDestination(name="Backup SSD", kind="Local")],
        last_backup_at="2026-05-28T06:00:00Z",
        last_backup_hours_ago=6.0,
        in_progress=False,
        excluded_pod_paths=[],
    )
    base.update(overrides)
    return LocalBackupStatus(**base)


# ─── collect_signals ───────────────────────────────────────────────────────

def test_collect_silent_on_healthy_state():
    s = _status()
    assert lbs.collect_signals(s) == []


def test_collect_silent_on_non_macos():
    s = _status(available=False, configured=False, destinations=[])
    assert lbs.collect_signals(s) == []


def test_collect_fires_not_configured():
    s = _status(configured=False, destinations=[], last_backup_at=None, last_backup_hours_ago=None)
    specs = lbs.collect_signals(s)
    assert len(specs) == 1
    assert specs[0]["type"] == "local_backup_not_configured"
    assert specs[0]["severity"] == "info"
    assert specs[0]["scope"] == "pod"


def test_collect_does_not_fire_other_signals_when_not_configured():
    # Even if exclusions are present, the not_configured Signal is the
    # actionable one — we don't pile up follow-on signals that won't help
    # the operator until they configure TM.
    s = _status(configured=False, destinations=[], excluded_pod_paths=["/Users/Shared/evolve"])
    specs = lbs.collect_signals(s)
    types = {sp["type"] for sp in specs}
    assert types == {"local_backup_not_configured"}


def test_collect_fires_stale_warn_at_48h():
    s = _status(last_backup_hours_ago=48.5)
    specs = lbs.collect_signals(s)
    assert len(specs) == 1
    assert specs[0]["type"] == "local_backup_stale"
    assert specs[0]["severity"] == "warn"


def test_collect_silent_below_warn_threshold():
    s = _status(last_backup_hours_ago=47.0)
    assert lbs.collect_signals(s) == []


def test_collect_escalates_to_alert_at_7d():
    s = _status(last_backup_hours_ago=24 * 7 + 1)
    specs = lbs.collect_signals(s)
    assert specs[0]["severity"] == "alert"


def test_collect_treats_never_backed_up_as_stale():
    # Destination set but no backup ever ran → still alert-worthy because
    # configured-but-broken is worse than not-configured (operator might
    # think TM is protecting them when it isn't).
    s = _status(last_backup_at=None, last_backup_hours_ago=None)
    specs = lbs.collect_signals(s)
    assert any(sp["type"] == "local_backup_stale" for sp in specs)


def test_collect_fires_workspace_excluded():
    s = _status(excluded_pod_paths=["/Users/Shared/evolve"])
    specs = lbs.collect_signals(s)
    assert len(specs) == 1
    assert specs[0]["type"] == "local_backup_workspace_excluded"
    assert specs[0]["severity"] == "alert"
    assert "/Users/Shared/evolve" in specs[0]["body"]


def test_collect_fires_both_stale_and_excluded():
    s = _status(
        last_backup_hours_ago=50.0,
        excluded_pod_paths=["/Users/Shared/evolve"],
    )
    specs = lbs.collect_signals(s)
    types = {sp["type"] for sp in specs}
    assert types == {"local_backup_stale", "local_backup_workspace_excluded"}


# ─── Signal body / details checks ──────────────────────────────────────────

def test_not_configured_body_includes_settings_deeplink():
    spec = lbs.build_signal_not_configured()
    assert spec["details"]["settings_deeplink"].startswith("x-apple.systempreferences:")


def test_stale_signal_records_thresholds():
    spec = lbs.build_signal_stale(hours_ago=72, last_backup_at="2026-05-25T06:00:00Z")
    assert spec["details"]["warn_threshold_hours"] == 48
    assert spec["details"]["alert_threshold_hours"] == 24 * 7


def test_workspace_excluded_lists_all_paths_in_details():
    spec = lbs.build_signal_workspace_excluded(["/a", "/b"])
    assert spec["details"]["excluded_paths"] == ["/a", "/b"]
    assert "/a" in spec["body"] and "/b" in spec["body"]


# ─── End-to-end run() ──────────────────────────────────────────────────────

def test_run_fires_and_writes_signal(tmp_path):
    s = _status(configured=False, destinations=[], last_backup_at=None, last_backup_hours_ago=None)
    kept, n_fired, n_resolved = lbs.run(
        tmp_path, [],
        status_getter=lambda pod_paths=None: s,
    )
    assert n_fired == 1
    assert n_resolved == 0
    sigs = list(signals_store.iter_active(tmp_path, producer="local_backup_signal"))
    assert len(sigs) == 1
    assert sigs[0].type == "local_backup_not_configured"


def test_run_dry_run_does_not_write(tmp_path):
    s = _status(configured=False, destinations=[], last_backup_at=None, last_backup_hours_ago=None)
    _, n_fired, _ = lbs.run(
        tmp_path, [],
        status_getter=lambda pod_paths=None: s,
        dry_run=True,
    )
    assert n_fired == 1
    assert list(signals_store.iter_active(tmp_path, producer="local_backup_signal")) == []


def test_run_sweep_resolve_archives_when_configured(tmp_path):
    # Pass 1: not configured — Signal fires.
    s1 = _status(configured=False, destinations=[], last_backup_at=None, last_backup_hours_ago=None)
    lbs.run(tmp_path, [], status_getter=lambda pod_paths=None: s1)
    assert len(list(signals_store.iter_active(tmp_path, producer="local_backup_signal"))) == 1

    # Pass 2: operator configured TM — sweep_resolve archives.
    s2 = _status()  # healthy
    kept, n_fired, n_resolved = lbs.run(
        tmp_path, [], status_getter=lambda pod_paths=None: s2,
    )
    assert kept == set()
    assert n_fired == 0
    assert n_resolved == 1
    assert list(signals_store.iter_active(tmp_path, producer="local_backup_signal")) == []


def test_run_emits_two_signals_when_both_conditions_present(tmp_path):
    s = _status(
        last_backup_hours_ago=200.0,  # alert range
        excluded_pod_paths=["/Users/Shared/evolve"],
    )
    kept, n_fired, _ = lbs.run(
        tmp_path, [], status_getter=lambda pod_paths=None: s,
    )
    assert n_fired == 2
    sigs = list(signals_store.iter_active(tmp_path, producer="local_backup_signal"))
    types = sorted(sg.type for sg in sigs)
    assert types == ["local_backup_stale", "local_backup_workspace_excluded"]


def test_run_silent_on_non_macos(tmp_path):
    s = _status(available=False, configured=False, destinations=[])
    _, n_fired, n_resolved = lbs.run(
        tmp_path, [], status_getter=lambda pod_paths=None: s,
    )
    assert n_fired == 0
    assert n_resolved == 0
    assert list(signals_store.iter_active(tmp_path, producer="local_backup_signal")) == []


def test_run_severity_escalates_warn_to_alert_on_re_observation(tmp_path):
    """Stale 48h → warn; later stale 7d → escalates to alert on the existing Signal."""
    s_warn = _status(last_backup_hours_ago=50.0)
    lbs.run(tmp_path, [], status_getter=lambda pod_paths=None: s_warn)
    sigs = list(signals_store.iter_active(tmp_path, producer="local_backup_signal"))
    assert sigs[0].severity == "warn"

    s_alert = _status(last_backup_hours_ago=24 * 8.0)
    lbs.run(tmp_path, [], status_getter=lambda pod_paths=None: s_alert)
    sigs2 = list(signals_store.iter_active(tmp_path, producer="local_backup_signal"))
    assert len(sigs2) == 1  # same Signal (same signature), re-observed
    assert sigs2[0].severity == "alert"
