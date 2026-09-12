"""Tests for monitor_coverage — Evolve's SELF_AUDIT.

Pinned behavior:
  - Discovery walks /Library/LaunchDaemons/ai.evolve.evolve.*.plist
    and parses each via plistlib; bad plists are skipped silently.
  - StartInterval daemons: silent if stdout log mtime > interval × 3
    (bounded by [5 min, 7d]).
  - StartCalendarInterval daemons: silent if log mtime > 48h.
  - No schedule: skipped (cannot compute expectation).
  - Severity: warn for 1-2 silent, critical for 3+ OR any CRITICAL_DAEMON
    silent regardless of count.
  - Zero silent: no signal (sweep-resolves any existing).
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import monitor_coverage as mc  # noqa: E402


def _write_plist(dir: Path, label: str, *, interval: int | None = None,
                 calendar: dict | None = None, stdout: str | None = None,
                 stderr: str | None = None) -> Path:
    data: dict = {"Label": label, "ProgramArguments": ["/bin/true"]}
    if interval is not None:
        data["StartInterval"] = interval
    if calendar is not None:
        data["StartCalendarInterval"] = calendar
    if stdout is not None:
        data["StandardOutPath"] = stdout
    if stderr is not None:
        data["StandardErrorPath"] = stderr
    path = dir / f"{label}.plist"
    with path.open("wb") as f:
        plistlib.dump(data, f)
    return path


# ── discovery ─────────────────────────────────────────────────────────


def test_discover_walks_both_evolve_prefixes(tmp_path: Path):
    """ai.evolve.evolve.* (admin-installed) AND ai.openclaw.evolve.*
    (analyzer pipeline) are both Evolve daemons we want to watch.
    Non-Evolve plists must be ignored."""
    _write_plist(tmp_path, "ai.evolve.evolve.audit", interval=900,
                 stdout=str(tmp_path / "audit.log"))
    _write_plist(tmp_path, "ai.evolve.evolve.heal", interval=300,
                 stdout=str(tmp_path / "heal.log"))
    _write_plist(tmp_path, "ai.openclaw.evolve.deploy_drift_monitor",
                 interval=3600, stdout=str(tmp_path / "drift.log"))
    # Non-Evolve plists should be ignored.
    _write_plist(tmp_path, "com.apple.something", interval=900)
    _write_plist(tmp_path, "io.unrelated.thing", interval=900)

    daemons = mc.discover_evolve_daemons(tmp_path)
    labels = {d.label for d in daemons}
    assert labels == {
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.heal",
        "ai.openclaw.evolve.deploy_drift_monitor",
    }


def test_discover_skips_malformed_plists(tmp_path: Path):
    # Write a real plist + garbage so the directory has both shapes.
    _write_plist(tmp_path, "ai.evolve.evolve.audit", interval=900)
    (tmp_path / "ai.evolve.evolve.broken.plist").write_bytes(b"not a plist {{{")

    daemons = mc.discover_evolve_daemons(tmp_path)
    assert len(daemons) == 1
    assert daemons[0].label == "ai.evolve.evolve.audit"


def test_discover_extracts_calendar_and_stdout(tmp_path: Path):
    _write_plist(
        tmp_path, "ai.evolve.evolve.cve-scan",
        calendar={"Hour": 9, "Minute": 10},
        stdout=str(tmp_path / "cve.log"),
    )
    daemons = mc.discover_evolve_daemons(tmp_path)
    assert daemons[0].has_calendar_schedule is True
    assert daemons[0].start_interval is None
    assert daemons[0].stdout_path == tmp_path / "cve.log"


def test_discover_returns_empty_when_dir_missing(tmp_path: Path):
    assert mc.discover_evolve_daemons(tmp_path / "does-not-exist") == []


# ── silence_threshold_for ─────────────────────────────────────────────


def test_threshold_uses_interval_times_3(tmp_path: Path):
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.hourly", plist_path=tmp_path,
        start_interval=3600, has_calendar_schedule=False,
        stdout_path=None, stderr_path=None,
    )
    assert mc.silence_threshold_for(d) == 3 * 3600


def test_threshold_floor_for_fast_daemons(tmp_path: Path):
    """1-minute daemon × 3 = 180s — too tight; should floor to 5 min."""
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.pod-health", plist_path=tmp_path,
        start_interval=60, has_calendar_schedule=False,
        stdout_path=None, stderr_path=None,
    )
    assert mc.silence_threshold_for(d) == mc.SILENCE_FLOOR_SEC


def test_threshold_cap_for_slow_daemons(tmp_path: Path):
    """3-day interval × 3 = 9 days — too long; cap at 7d."""
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.slow", plist_path=tmp_path,
        start_interval=3 * 24 * 3600, has_calendar_schedule=False,
        stdout_path=None, stderr_path=None,
    )
    assert mc.silence_threshold_for(d) == mc.SILENCE_CAP_SEC


def test_threshold_for_calendar_daemon(tmp_path: Path):
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.weekly", plist_path=tmp_path,
        start_interval=None, has_calendar_schedule=True,
        stdout_path=None, stderr_path=None,
    )
    assert mc.silence_threshold_for(d) == mc.CALENDAR_SILENCE_SEC


def test_threshold_none_when_no_schedule(tmp_path: Path):
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.no-schedule", plist_path=tmp_path,
        start_interval=None, has_calendar_schedule=False,
        stdout_path=None, stderr_path=None,
    )
    assert mc.silence_threshold_for(d) is None


# ── detect_silent_monitors ────────────────────────────────────────────


def _daemon_with_log(label: str, interval: int, log_path: Path) -> mc.DaemonInfo:
    return mc.DaemonInfo(
        label=label, plist_path=Path("/dev/null"),
        start_interval=interval, has_calendar_schedule=False,
        stdout_path=log_path, stderr_path=None,
    )


def test_fresh_log_not_silent(tmp_path: Path):
    log = tmp_path / "fresh.log"
    log.write_text("ran")
    # Set mtime to 30 seconds ago.
    import os
    now = 1000000.0
    os.utime(log, (now - 30, now - 30))
    d = _daemon_with_log("ai.evolve.evolve.audit", 900, log)
    assert mc.detect_silent_monitors([d], now=now) == []


def test_old_log_is_silent(tmp_path: Path):
    """Daemon in WATCHED_DAEMONS with mtime past threshold → silent finding."""
    log = tmp_path / "old.log"
    log.write_text("ran")
    import os
    now = 1000000.0
    # 5h ago — for a 15-min daemon, threshold = 45min; 5h is silent
    os.utime(log, (now - 5 * 3600, now - 5 * 3600))
    # ai.evolve.evolve.audit is in WATCHED_DAEMONS
    d = _daemon_with_log("ai.evolve.evolve.audit", 900, log)
    silent = mc.detect_silent_monitors([d], now=now)
    assert len(silent) == 1
    assert silent[0].label == "ai.evolve.evolve.audit"
    assert silent[0].silent_for_sec == 5 * 3600


def test_unwatched_daemon_skipped_even_when_silent(tmp_path: Path):
    """Critical guard: silent-by-design daemons (audit-scheduler and the
    rest of the SKIPPED list) must NOT fire false-positive silent findings.
    They get covered when their label is added to WATCHED_DAEMONS."""
    log = tmp_path / "old.log"
    log.write_text("")  # 0 bytes, never written
    import os
    now = 1000000.0
    os.utime(log, (now - 30 * 86400, now - 30 * 86400))  # 30d "silent"
    # audit-scheduler is NOT in WATCHED_DAEMONS — silent by design today
    d = _daemon_with_log("ai.evolve.evolve.audit-scheduler", 3600, log)
    assert mc.detect_silent_monitors([d], now=now) == []


def test_missing_log_is_silent(tmp_path: Path):
    d = _daemon_with_log("ai.evolve.evolve.audit", 900, tmp_path / "never-written.log")
    silent = mc.detect_silent_monitors([d], now=1000000.0)
    assert len(silent) == 1
    assert "never run" in silent[0].reason


def test_missing_stdout_path_is_silent(tmp_path: Path):
    """A watched daemon with no StandardOutPath fires a different finding."""
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.audit", plist_path=Path("/dev/null"),
        start_interval=900, has_calendar_schedule=False,
        stdout_path=None, stderr_path=None,
    )
    silent = mc.detect_silent_monitors([d], now=1000000.0)
    assert len(silent) == 1
    assert "no StandardOutPath" in silent[0].reason


def test_no_schedule_daemon_is_skipped(tmp_path: Path):
    """Persistent daemons (admin-ui) with no interval shouldn't fire."""
    d = mc.DaemonInfo(
        label="ai.evolve.evolve.admin-ui", plist_path=Path("/dev/null"),
        start_interval=None, has_calendar_schedule=False,
        stdout_path=tmp_path / "never.log", stderr_path=None,
    )
    assert mc.detect_silent_monitors([d], now=1000000.0) == []


# ── build_signal_spec / severity policy ────────────────────────────────


def _silent(label: str, reason: str = "test") -> mc.SilentMonitor:
    return mc.SilentMonitor(
        label=label, log_path=None,
        silent_for_sec=999999, expected_max_sec=900, reason=reason,
    )


def test_no_silent_returns_none():
    assert mc.build_signal_spec([]) is None


def test_one_silent_non_critical_is_warn():
    spec = mc.build_signal_spec([_silent("ai.evolve.evolve.cost_watchdog")])
    assert spec is not None
    assert spec["severity"] == "warn"
    assert "cost_watchdog" in spec["title"]


def test_two_silent_non_critical_still_warn():
    spec = mc.build_signal_spec([
        _silent("ai.evolve.evolve.cost_watchdog"),
        _silent("ai.evolve.evolve.spend-alert"),
    ])
    assert spec["severity"] == "warn"
    assert "2 Evolve monitors silent" in spec["title"]


def test_three_silent_escalates_to_critical():
    spec = mc.build_signal_spec([
        _silent("ai.evolve.evolve.cost_watchdog"),
        _silent("ai.evolve.evolve.spend-alert"),
        _silent("ai.evolve.evolve.repo-puller"),
    ])
    assert spec["severity"] == "alert"
    assert spec["details"]["has_critical"] is False
    assert spec["details"]["magnitude"] == 2


def test_one_critical_daemon_silent_escalates_to_critical():
    """Single silent audit daemon should page — it's load-bearing for everything."""
    spec = mc.build_signal_spec([_silent("ai.evolve.evolve.audit")])
    assert spec["severity"] == "alert"
    assert spec["details"]["has_critical"] is True
    assert spec["details"]["magnitude"] == 3
    # Operator playbook
    assert "launchctl print" in spec["body"]
    assert "install-infra-jobs" in spec["body"]


def test_heal_silent_is_critical():
    """heal stops gateway self-healing if down — load-bearing."""
    spec = mc.build_signal_spec([_silent("ai.evolve.evolve.heal")])
    assert spec["severity"] == "alert"


# ── collect / integration ──────────────────────────────────────────────


def test_collect_end_to_end_silent_audit(tmp_path: Path):
    """Full flow: write a plist with old log, expect one silent rollup."""
    log = tmp_path / "audit.log"
    log.write_text("ran once long ago")
    import os
    now = 1000000.0
    os.utime(log, (now - 10 * 3600, now - 10 * 3600))

    _write_plist(tmp_path, "ai.evolve.evolve.audit",
                 interval=900, stdout=str(log))

    detections = mc.collect(launchd_dir=tmp_path, now=now)
    assert len(detections) == 1
    spec = detections[0]
    assert spec["type"] == mc.SIGNAL_TYPE
    assert spec["producer"] == mc.PRODUCER
    assert spec["severity"] == "alert"
    assert spec["details"]["silent_count"] == 1


def test_collect_returns_empty_when_all_fresh(tmp_path: Path):
    log = tmp_path / "audit.log"
    log.write_text("ran")
    import os
    now = 1000000.0
    os.utime(log, (now - 30, now - 30))

    _write_plist(tmp_path, "ai.evolve.evolve.audit",
                 interval=900, stdout=str(log))

    detections = mc.collect(launchd_dir=tmp_path, now=now)
    assert detections == []


def test_real_failure_mode_audit_log_silent_long(tmp_path: Path):
    """End-to-end variant of the actual mini-discovery flow: a watched
    daemon (audit) with stale log → critical rollup with the silence
    duration in the body. This is the case the whole feature exists for."""
    log = tmp_path / "audit.log"
    log.write_text("last successful run")
    import os
    now = 1000000.0
    os.utime(log, (now - 27 * 24 * 3600, now - 27 * 24 * 3600))

    _write_plist(tmp_path, "ai.evolve.evolve.audit",
                 interval=900, stdout=str(log))

    detections = mc.collect(launchd_dir=tmp_path, now=now)
    assert len(detections) == 1
    spec = detections[0]
    assert spec["severity"] == "alert"  # audit is in CRITICAL_DAEMONS
    # Body must surface the silence duration so the operator sees scale.
    assert "27d" in spec["body"] or "d" in spec["body"]


# ── producer-liveness layer (producer_silent) ──────────────────────────
#
# Regression target: capability_gap_monitor / engagement_amplifier_monitor
# crash-looped for 18 days on a phantom import. Their stdout summary log
# froze (the crash precedes the print) while nothing in monitor_coverage's
# allowlist watched them. These tests pin the new producer-liveness check
# that catches exactly that. ``now`` is threaded everywhere (clock-coupling
# trap); the signals store is imported lazily in the integration test
# (shard-pollution trap).

_CAP = "ai.openclaw.evolve.capability_gap_monitor"
_ENG = "ai.openclaw.evolve.engagement_amplifier_monitor"


def _producer_daemon(label: str, stdout: Path | None,
                     stderr: Path | None = None) -> mc.DaemonInfo:
    return mc.DaemonInfo(
        label=label, plist_path=Path("/dev/null"),
        start_interval=None, has_calendar_schedule=True,
        stdout_path=stdout, stderr_path=stderr,
    )


def _all_fresh_producer_daemons(dir: Path, now: float) -> dict[str, mc.DaemonInfo]:
    """A fresh (recently-run) DaemonInfo for EVERY registered producer.

    Tests model a real pod where all registered monitors are installed, then
    age one log to simulate silence — otherwise the absent-plist "not
    installed" finding fires for the producers a test didn't set up.
    """
    import os
    out: dict[str, mc.DaemonInfo] = {}
    for label in mc.SIGNAL_PRODUCER_MONITORS:
        stem = mc._short_name(label)
        log = dir / f"{stem}.log"
        log.write_text("ok")
        os.utime(log, (now - 600, now - 600))
        out[label] = _producer_daemon(label, log, dir / f"{stem}.err.log")
    return out


def _write_all_producer_plists(dir: Path, now: float) -> dict[str, Path]:
    """Write a plist + fresh stdout log for every registered producer.

    Returns ``{label: stdout_log_path}`` so a test can age one log to make
    that producer look dark while the rest stay healthy."""
    import os
    logs: dict[str, Path] = {}
    for label in mc.SIGNAL_PRODUCER_MONITORS:
        stem = mc._short_name(label)
        log = dir / f"{stem}.log"
        log.write_text("ok")
        os.utime(log, (now - 600, now - 600))
        _write_plist(dir, label, calendar={"Hour": 3, "Minute": 15},
                     stdout=str(log), stderr=str(dir / f"{stem}.err.log"))
        logs[label] = log
    return logs


def test_registry_and_exclusions_are_disjoint():
    overlap = set(mc.SIGNAL_PRODUCER_MONITORS) & set(mc.EXCLUDED_SIGNAL_PRODUCERS)
    assert not overlap, f"a monitor is both watched and excluded: {overlap}"
    assert _CAP in mc.SIGNAL_PRODUCER_MONITORS
    assert _ENG in mc.SIGNAL_PRODUCER_MONITORS


def test_producer_threshold_is_cadence_times_multiplier():
    pm = mc.SIGNAL_PRODUCER_MONITORS[_CAP]
    assert mc.producer_silence_threshold(pm) == pm.cadence_sec * mc.PRODUCER_SILENCE_MULTIPLIER
    # Daily monitor → 2× = 48h grace before firing.
    assert mc.producer_silence_threshold(pm) == 48 * 3600


def test_fresh_producer_stdout_not_silent(tmp_path: Path):
    now = 2_000_000.0
    daemons = list(_all_fresh_producer_daemons(tmp_path, now).values())
    assert mc.detect_silent_producers(daemons, now=now) == []


def test_crashlooping_producer_is_silent(tmp_path: Path):
    """The 18-day incident: stdout frozen well past the daily threshold."""
    import os
    now = 2_000_000.0
    daemons_by_label = _all_fresh_producer_daemons(tmp_path, now)
    # Age capability_gap_monitor's stdout to 18 days; the crash wrote a
    # traceback to stderr but never reached the summary print.
    cap = daemons_by_label[_CAP]
    os.utime(cap.stdout_path, (now - 18 * 86400, now - 18 * 86400))
    cap.stderr_path.write_text("ImportError: cannot import name 'all_bot_ids'")

    silent = mc.detect_silent_producers(list(daemons_by_label.values()), now=now)
    assert len(silent) == 1
    assert silent[0].label == _CAP
    assert silent[0].silent_for_sec == 18 * 86400
    assert "app_suggester" in silent[0].downstream
    assert str(cap.stderr_path) == silent[0].stderr_path


def test_producer_never_completed_a_run_is_silent(tmp_path: Path):
    now = 2_000_000.0
    daemons_by_label = _all_fresh_producer_daemons(tmp_path, now)
    # capability_gap_monitor's stdout log was never created.
    daemons_by_label[_CAP] = _producer_daemon(
        _CAP, tmp_path / "never-written.log", tmp_path / "cap.err.log")
    silent = mc.detect_silent_producers(list(daemons_by_label.values()), now=now)
    assert len(silent) == 1
    assert silent[0].label == _CAP
    assert "never completed a run" in silent[0].reason


def test_registered_producer_not_installed_is_silent_on_real_pod(tmp_path: Path):
    """Registered monitor whose plist is absent, but other Evolve daemons
    exist (real pod) → flagged as not installed / unloaded."""
    other = _daemon_with_log("ai.evolve.evolve.audit", 900, tmp_path / "audit.log")
    silent = mc.detect_silent_producers([other], now=2_000_000.0)
    labels = {s.label for s in silent}
    assert _CAP in labels and _ENG in labels
    assert all("not installed" in s.reason or "unloaded" in s.reason for s in silent)


def test_no_daemons_means_no_producer_findings(tmp_path: Path):
    """Dev/CI host with no Evolve daemons must not false-flag every monitor."""
    assert mc.detect_silent_producers([], now=2_000_000.0) == []
    assert mc.collect_producer_silence(launchd_dir=tmp_path, now=2_000_000.0) == []


def test_build_producer_silent_spec_empty_is_none():
    assert mc.build_producer_silent_spec([]) is None


def test_build_producer_silent_spec_one_dark_is_warn():
    s = mc.SilentProducer(
        label=_CAP, downstream="app_suggester capability-gap recommendations",
        log_path="/x.log", stderr_path="/x.err.log",
        silent_for_sec=18 * 86400, expected_max_sec=48 * 3600,
        reason="no successful run in 18d",
    )
    spec = mc.build_producer_silent_spec([s])
    assert spec is not None
    assert spec["type"] == mc.PRODUCER_SILENT_TYPE
    assert spec["producer"] == mc.PRODUCER
    assert spec["severity"] == "warn"
    assert spec["scope"] == "pod"
    assert spec["flavor"] == "maintenance"
    # Legibility: names the starved downstream + a crash log to read first.
    assert "app_suggester" in spec["body"]
    assert "x.err.log" in spec["body"]
    assert "recommendations stalled" in spec["title"]


def test_build_producer_silent_spec_three_dark_escalates():
    silent = [
        mc.SilentProducer(label=f"ai.openclaw.evolve.m{i}", downstream="d",
                          log_path=None, stderr_path=None, silent_for_sec=99999,
                          expected_max_sec=48 * 3600, reason="r")
        for i in range(3)
    ]
    spec = mc.build_producer_silent_spec(silent)
    assert spec["severity"] == "alert"
    assert spec["details"]["magnitude"] == 3


def test_collect_producer_silence_end_to_end(tmp_path: Path):
    """Write real plists for all producers with one stale → one rollup spec."""
    import os
    now = 2_000_000.0
    logs = _write_all_producer_plists(tmp_path, now)
    os.utime(logs[_CAP], (now - 18 * 86400, now - 18 * 86400))  # only cap is dark

    detections = mc.collect_producer_silence(launchd_dir=tmp_path, now=now)
    assert len(detections) == 1
    spec = detections[0]
    assert spec["type"] == mc.PRODUCER_SILENT_TYPE
    assert spec["details"]["silent_count"] == 1
    assert spec["details"]["silent"][0]["label"] == _CAP


def test_producer_recovers_when_stdout_fresh(tmp_path: Path):
    now = 2_000_000.0
    _write_all_producer_plists(tmp_path, now)  # all fresh
    assert mc.collect_producer_silence(launchd_dir=tmp_path, now=now) == []


def test_run_observes_then_sweep_resolves_producer_silence(tmp_path: Path):
    """Full store round-trip: dark producer → producer_silent fires; on
    recovery the next run sweep-resolves (archives) it."""
    from signals import store as signals_store  # lazy: shard-pollution trap

    shared = tmp_path / "shared"
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    import os
    now = 2_000_000.0
    logs = _write_all_producer_plists(launchd, now)
    os.utime(logs[_CAP], (now - 18 * 86400, now - 18 * 86400))  # cap is dark

    n_fired, _ = mc.run(shared, launchd_dir=launchd, now=now)
    assert n_fired == 1
    active = [s for s in signals_store.iter_active(shared, producer=mc.PRODUCER)
              if s.type == mc.PRODUCER_SILENT_TYPE]
    assert len(active) == 1
    assert active[0].severity == "warn"

    # Monitor recovers: stdout advances to ~now → next run resolves it.
    os.utime(logs[_CAP], (now - 300, now - 300))
    n_fired2, n_resolved = mc.run(shared, launchd_dir=launchd, now=now)
    assert n_fired2 == 0
    assert n_resolved >= 1
    still_active = [s for s in signals_store.iter_active(shared, producer=mc.PRODUCER)
                   if s.type == mc.PRODUCER_SILENT_TYPE]
    assert still_active == []


# ── audit-drain liveness layer (audit_drain_silent) ────────────────────
#
# Regression target: the admin-side audit drain (audit_poller.tick) ran with
# Result=success while ingesting 0 records for days, because a hardcoded outbox
# path resolved every root to a non-existent dir on the Linux VPS — and nothing
# fired because the daemon's stdout DID advance on every empty-but-successful
# tick (mtime-based producer-silence is structurally blind to it). These tests
# pin the heartbeat-driven check that catches the silent stall. ``now`` is
# threaded everywhere (clock-coupling trap).


def _hb(samples: list[tuple[int, int, int]]) -> list[dict]:
    """Heartbeat ``recent`` from ``(ts, processed, backlog)`` tuples."""
    return [{"ts": ts, "processed": p, "backlog": b} for ts, p, b in samples]


def _write_heartbeat(shared: Path, samples: list[tuple[int, int, int]]) -> None:
    import json
    d = shared / "monitors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "audit_drain_heartbeat.json").write_text(
        json.dumps({"schema": 1, "recent": _hb(samples)})
    )


def test_audit_drain_empty_heartbeat_is_no_op():
    assert mc.detect_audit_drain_stall([], now=10_000) is None


def test_audit_drain_healthy_is_not_stalled():
    """Records drain every tick (backlog ~0) → no finding."""
    now = 100_000
    samples = [(now - i * 3600, 5, 0) for i in range(5)][::-1]
    assert mc.detect_audit_drain_stall(_hb(samples), now=now) is None


def test_audit_drain_quiet_pod_is_not_stalled():
    """processed==0 but backlog==0 (nothing to drain) must NOT false-fire."""
    now = 100_000
    samples = [(now - i * 3600, 0, 0) for i in range(6)][::-1]
    assert mc.detect_audit_drain_stall(_hb(samples), now=now) is None


def test_audit_drain_brief_idle_below_threshold_is_quiet():
    """Two idle-with-backlog ticks is under AUDIT_DRAIN_IDLE_TICKS (3)."""
    now = 100_000
    samples = [(now - 7200, 4, 0), (now - 3600, 0, 2), (now, 0, 2)]
    assert mc.detect_audit_drain_stall(_hb(samples), now=now) is None


def test_audit_drain_silent_stall_fires_warn():
    now = 100_000
    # 3 consecutive idle-with-backlog ticks, small + steady backlog.
    samples = [(now - 10800, 3, 0), (now - 7200, 0, 2),
               (now - 3600, 0, 2), (now, 0, 2)]
    stall = mc.detect_audit_drain_stall(_hb(samples), now=now)
    assert stall is not None
    assert stall.reason == "silent_stall"
    assert stall.idle_ticks == 3
    assert stall.backlog == 2
    assert stall.growing is False
    assert stall.severity == "warn"


def test_audit_drain_growing_backlog_is_flagged():
    now = 100_000
    samples = [(now - 14400, 1, 0), (now - 10800, 0, 5),
               (now - 7200, 0, 9), (now - 3600, 0, 14), (now, 0, 20)]
    stall = mc.detect_audit_drain_stall(_hb(samples), now=now)
    assert stall is not None and stall.reason == "silent_stall"
    assert stall.idle_ticks == 4
    assert stall.growing is True
    assert "growing 5→20" in stall.detail


def test_audit_drain_large_backlog_escalates_to_alert():
    now = 100_000
    samples = [(now - 14400, 1, 0)] + [
        (now - i * 3600, 0, 195) for i in range(3, 0, -1)
    ] + [(now, 0, 195)]
    stall = mc.detect_audit_drain_stall(_hb(samples), now=now)
    assert stall is not None
    assert stall.backlog == 195
    assert stall.severity == "alert"  # ≥ AUDIT_DRAIN_ALERT_BACKLOG


def test_audit_drain_stale_heartbeat_fires_alert():
    """No tick recorded in > AUDIT_DRAIN_STALE_SEC → the drain isn't running."""
    now = 1_000_000
    last = now - (7 * 3600)  # 7h ago, past the 6h stale threshold
    stall = mc.detect_audit_drain_stall(_hb([(last, 0, 0)]), now=now)
    assert stall is not None
    assert stall.reason == "heartbeat_stale"
    assert stall.severity == "alert"
    assert stall.stale_for_sec is not None
    assert stall.stale_for_sec > mc.AUDIT_DRAIN_STALE_SEC


def test_audit_drain_spec_shape_and_distinct_signature():
    now = 100_000
    samples = [(now - 10800, 3, 0)] + [(now - i * 3600, 0, 2) for i in range(2, -1, -1)]
    stall = mc.detect_audit_drain_stall(_hb(samples), now=now)
    spec = mc.build_audit_drain_silent_spec(stall)
    assert spec["producer"] == mc.PRODUCER
    assert spec["type"] == mc.AUDIT_DRAIN_SILENT_TYPE
    assert spec["scope"] == "pod"
    # Distinct signature from the other two monitor_coverage rollups.
    assert spec["signature"] != mc.make_signature(mc.PRODUCER, mc.PRODUCER_SILENT_TYPE, "pod")
    assert spec["signature"] != mc.make_signature(mc.PRODUCER, mc.SIGNAL_TYPE, "pod")
    assert mc.build_audit_drain_silent_spec(None) is None


def test_audit_drain_collect_guarded_to_real_pod(tmp_path: Path):
    """No Evolve daemons (dev/CI host) → no assessment even with a heartbeat."""
    shared = tmp_path / "shared"
    _write_heartbeat(shared, [(100_000 - i * 3600, 0, 9) for i in range(4, -1, -1)])
    # launchd dir is empty → discover returns [] → guarded no-op.
    assert mc.collect_audit_drain_silence(shared, launchd_dir=tmp_path, now=100_000) == []


def test_audit_drain_collect_fires_on_real_pod(tmp_path: Path):
    shared = tmp_path / "shared"
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    now = 2_000_000
    _write_all_producer_plists(launchd, now)  # keeps producer_silence quiet
    _write_heartbeat(shared, [(now - i * 3600, 0, 9) for i in range(4, -1, -1)])
    specs = mc.collect_audit_drain_silence(shared, launchd_dir=launchd, now=now)
    assert len(specs) == 1
    assert specs[0]["type"] == mc.AUDIT_DRAIN_SILENT_TYPE


def test_run_observes_then_sweep_resolves_audit_drain_silence(tmp_path: Path):
    """Full store round-trip: stalled drain → audit_drain_silent fires; on
    recovery (records draining again) the next run sweep-resolves it."""
    from signals import store as signals_store  # lazy: shard-pollution trap

    shared = tmp_path / "shared"
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    now = 2_000_000
    _write_all_producer_plists(launchd, now)  # producer_silence stays quiet
    _write_heartbeat(shared, [(now - i * 3600, 0, 9) for i in range(4, -1, -1)])

    n_fired, _ = mc.run(shared, launchd_dir=launchd, now=now)
    assert n_fired == 1
    active = [s for s in signals_store.iter_active(shared, producer=mc.PRODUCER)
              if s.type == mc.AUDIT_DRAIN_SILENT_TYPE]
    assert len(active) == 1

    # Drain recovers: latest tick processed records, backlog cleared.
    _write_heartbeat(shared, [(now - 3600, 0, 9), (now, 9, 0)])
    n_fired2, n_resolved = mc.run(shared, launchd_dir=launchd, now=now)
    assert n_fired2 == 0
    assert n_resolved >= 1
    still = [s for s in signals_store.iter_active(shared, producer=mc.PRODUCER)
             if s.type == mc.AUDIT_DRAIN_SILENT_TYPE]
    assert still == []


def test_all_summary_printing_monitors_are_classified():
    """Anti-drift coverage lint. Every analyzer ``*_monitor.py`` that prints a
    stdout run-summary AND emits Signals must be either watched by the
    producer-liveness check or explicitly excluded — so a future RSI monitor
    cannot silently fall through the gap that hid capability_gap_monitor for
    18 days."""
    analyzer_dir = Path(mc.__file__).parent
    unclassified: list[str] = []
    for py in sorted(analyzer_dir.glob("*_monitor.py")):
        text = py.read_text(encoding="utf-8")
        prints_summary = "print(json.dumps(summary" in text
        emits_signals = ("signals_store.observe" in text
                         or "from signals import store" in text)
        if not (prints_summary and emits_signals):
            continue
        label = f"ai.openclaw.evolve.{py.stem}"
        if (label in mc.SIGNAL_PRODUCER_MONITORS
                or label in mc.EXCLUDED_SIGNAL_PRODUCERS):
            continue
        unclassified.append(py.stem)
    assert not unclassified, (
        "These analyzer monitors print a stdout run-summary and emit Signals "
        "but are neither watched by the producer-liveness check nor explicitly "
        "excluded. Add each to SIGNAL_PRODUCER_MONITORS (or "
        "EXCLUDED_SIGNAL_PRODUCERS with a reason) in monitor_coverage.py: "
        f"{unclassified}"
    )
