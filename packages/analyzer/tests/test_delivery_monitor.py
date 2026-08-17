"""tests/test_delivery_monitor.py — proactive-delivery monitor (U2.1).

Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §14.

Covers every row of the §6.2 tri-state matrix (including each
probe-failure → unmeasurable path — the no-`except: return ok` greps),
window math (calendar vs interval, TZ from plist, DST straddle),
effective-contract resolution (declared vs derived Option-A defaults),
the per-action state machine (escalation, late recovery, first-install,
operator-disabled, interval miss threshold), the §6.3 edge cases
(host-asleep overlap + allowance, gateway-down linkage), and the
run()-level Signal + ledger + sweep_resolve loop against a real
tmp-path Signal store.

House rules honored: probes are injected via the runner seam (never a
module-level subprocess patch); no multiprocessing.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import delivery_monitor as dm  # noqa: E402
from signals import store as signals_store  # noqa: E402


@pytest.fixture(autouse=True)
def _plist_dir(tmp_path_factory, monkeypatch):
    """Point the monitor's plist directory at a fixture dir carrying the
    standard test label's plist (TZ=America/New_York), so window math is
    deterministic regardless of the host timezone. Production reads the
    real /Library/LaunchDaemons the same way (direct read)."""
    pdir = tmp_path_factory.mktemp("launchd")
    (pdir / "ai.evolve.testbot.test-app.plist").write_text(_PLIST_WITH_TZ)
    # The run()-level tests resolve ${bot_id} against the fixture home
    # dir's account name ("home").
    (pdir / "ai.evolve.home.test-app.plist").write_text(_PLIST_WITH_TZ)
    monkeypatch.setattr(dm, "LAUNCHD_PLIST_DIR", pdir)
    return pdir


NY = ZoneInfo("America/New_York")

# A Tuesday. 07:00 NY fire + 30 min grace = window 07:00–07:30.
NOW = datetime(2026, 6, 9, 12, 5, 0, tzinfo=timezone.utc)  # 08:05 NY
WINDOW = dm.Window(
    start=datetime(2026, 6, 9, 7, 0, tzinfo=NY),
    end=datetime(2026, 6, 9, 7, 30, tzinfo=NY),
)

_PLIST_WITH_TZ = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.evolve.testbot.test-app</string>
  <key>EnvironmentVariables</key>
  <dict><key>TZ</key><string>America/New_York</string></dict>
</dict></plist>
"""

_LAUNCHCTL_LOADED = (0, '{\n\t"LastExitStatus" = 0;\n\t"PID" = 123;\n}', "")
_LAUNCHCTL_NOT_FOUND = (
    113, "", "Could not find service \"ai.evolve.testbot.test-app\" in domain for system",
)
_SUDO_DENIED = (1, "", "sudo: a password is required")


class FakeRunner:
    """Substring-keyed canned responses; default is file-not-found."""

    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None):
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, resp in self.responses.items():
            if key in joined:
                return resp
        return (1, "", "No such file or directory")


def _probes(responses: dict[str, tuple[int, str, str]] | None = None) -> dm.Probes:
    return dm.Probes(runner=FakeRunner(responses))


def _contract(**over) -> dm.EffectiveContract:
    kwargs = dict(
        user_facing=True,
        window_minutes=30,
        delivered={"kind": "run_file", "path": "memory/briefing-runs/{date}.json"},
        heal="none",
        source="declared",
    )
    kwargs.update(over)
    return dm.EffectiveContract(**kwargs)


def _action(workspace: Path, **over) -> dm.MonitoredAction:
    kwargs = dict(
        bot_id="testbot",
        app_id="test-app",
        app_name="Test App",
        action_id="test-action",
        mechanism="launchd_python_signal",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
        label="ai.evolve.testbot.test-app",
        workspace=workspace,
        contract=_contract(),
        action_state="active",
        installed_at="2026-06-01T00:00:00-04:00",
        schedule_human="07:00",
    )
    kwargs.update(over)
    return dm.MonitoredAction(**kwargs)


def _write_run_file(workspace: Path, sent_at: str, date_str: str = "2026-06-09") -> Path:
    runs = workspace / "memory" / "briefing-runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{date_str}.json"
    path.write_text(json.dumps({"date": date_str, "sent_at": sent_at}))
    return path


def _touch_wrapper_log(workspace: Path, action_id: str, mtime: datetime) -> Path:
    log_dir = workspace / "evolve" / "scheduled" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{action_id}.log"
    path.write_text("ran\n")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# §6.2 classification matrix (pure)
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_delivered_on_time():
    cls = dm.classify_window(
        window=WINDOW,
        delivered_at=datetime(2026, 6, 9, 7, 10, tzinfo=NY),
        delivered_probe_errors=[], ran=True, ran_probe_errors=[],
    )
    assert cls.outcome == dm.OUTCOME_ON_TIME
    assert cls.diagnosis is None


def test_classify_late_delivery_is_not_a_fourth_state():
    cls = dm.classify_window(
        window=WINDOW,
        delivered_at=datetime(2026, 6, 9, 7, 41, tzinfo=NY),
        delivered_probe_errors=[], ran=True, ran_probe_errors=[],
    )
    assert cls.outcome == dm.OUTCOME_LATE
    assert cls.delivered_at is not None


def test_classify_stale_evidence_from_prior_window_is_not_delivery():
    """Yesterday's run file must not greenlight today's window."""
    cls = dm.classify_window(
        window=WINDOW,
        delivered_at=datetime(2026, 6, 8, 7, 10, tzinfo=NY),
        delivered_probe_errors=[], ran=False, ran_probe_errors=[],
        loaded=True,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.diagnosis == dm.DIAGNOSIS_DID_NOT_RUN


def test_classify_did_not_run():
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=False, ran_probe_errors=[], loaded=True,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.diagnosis == dm.DIAGNOSIS_DID_NOT_RUN
    assert cls.suspected_cause is None


def test_classify_did_not_run_bootout_case_names_not_loaded():
    """The §13 proof-artifact shape: plist exists, label not loaded."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=False, ran_probe_errors=[], loaded=False,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "not_loaded"


def test_classify_ran_undelivered():
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[],
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.diagnosis == dm.DIAGNOSIS_RAN_UNDELIVERED


def test_classify_ran_undelivered_links_gateway_down():
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[],
        gateway_down_signal_id="sig-gw-1",
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "gateway_down"
    assert cls.caused_by_signal_id == "sig-gw-1"


def test_classify_delivered_probe_failure_is_unmeasurable_not_ok():
    """EACCES on the workspace must never coerce into 'looks fine'."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None,
        delivered_probe_errors=["sudo cat …: rc=1 a password is required"],
        ran=True, ran_probe_errors=[],
    )
    assert cls.outcome == dm.OUTCOME_UNMEASURABLE
    assert cls.probe_errors


def test_classify_delivered_probe_failure_never_claims_a_miss():
    """…and equally must never fabricate a did_not_run it can't evidence."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None,
        delivered_probe_errors=["EACCES"],
        ran=False, ran_probe_errors=[], loaded=True,
    )
    assert cls.outcome == dm.OUTCOME_UNMEASURABLE
    assert cls.diagnosis is None


def test_classify_scheduler_probe_failure_is_unmeasurable():
    """sudo -n denied on launchctl: can't assert did_not_run → (c)."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=None, ran_probe_errors=["launchctl list …: sudo denied"],
    )
    assert cls.outcome == dm.OUTCOME_UNMEASURABLE


def test_classify_positive_delivery_short_circuits_broken_probes():
    """An on-time run file proves delivery even when launchctl is broken."""
    cls = dm.classify_window(
        window=WINDOW,
        delivered_at=datetime(2026, 6, 9, 7, 5, tzinfo=NY),
        delivered_probe_errors=[],
        ran=None, ran_probe_errors=["launchctl list …: sudo denied"],
    )
    assert cls.outcome == dm.OUTCOME_ON_TIME


def test_classify_host_asleep_overlap_names_cause_and_links():
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=False, ran_probe_errors=[], loaded=True,
        slept_overlap_signal_id="sig-sleep-1",
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "host_asleep"
    assert cls.caused_by_signal_id == "sig-sleep-1"


def test_classify_dst_straddle_names_cause():
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=False, ran_probe_errors=[], loaded=True, dst=True,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "dst"


def test_classify_oc_cron_ran_inside_window():
    ms = int(datetime(2026, 6, 9, 7, 4, tzinfo=NY).timestamp() * 1000)
    ran, errs = dm.classify_oc_cron_ran({"state": {"lastRunAtMs": ms}}, WINDOW)
    assert ran is True and errs == []


def test_classify_oc_cron_stale_last_run_means_no_fire():
    ms = int(datetime(2026, 6, 8, 7, 4, tzinfo=NY).timestamp() * 1000)
    ran, _ = dm.classify_oc_cron_ran({"state": {"lastRunAtMs": ms}}, WINDOW)
    assert ran is False


def test_classify_oc_cron_missing_job_is_no_fire():
    ran, _ = dm.classify_oc_cron_ran(None, WINDOW)
    assert ran is False


# ─────────────────────────────────────────────────────────────────────────────
# Exit-status + undeclared-evidence honesty (the OC-2026.6 masking fix —
# scheduler exit 1 + no run file must be a miss, never "unmeasurable")
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_nonzero_exit_is_a_miss_even_without_declared_evidence():
    """Eight days of exit-1 crashes read as 'unmeasurable' on the pod
    because undeclared evidence was treated as a probe error. A fire in
    the window plus a non-zero exit is a definite ran_undelivered."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[], last_exit=1,
        delivered_declared=False,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.diagnosis == dm.DIAGNOSIS_RAN_UNDELIVERED
    assert cls.suspected_cause == "script_error"
    assert cls.last_exit == 1


def test_classify_nonzero_exit_beats_declared_evidence_silence():
    """Declared run_file evidence absent + exit 1 names script_error
    instead of a cause-less ran_undelivered."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[], last_exit=2,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "script_error"
    assert cls.last_exit == 2


def test_classify_nonzero_exit_gateway_down_keeps_gateway_attribution():
    """gateway_down stays the named cause (it links the causing Signal
    and drives the deferred-rerun path) even when the exit was non-zero."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[], last_exit=1,
        gateway_down_signal_id="sig-gw-2",
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.suspected_cause == "gateway_down"
    assert cls.caused_by_signal_id == "sig-gw-2"


def test_classify_clean_exit_without_declared_evidence_is_unmeasurable():
    """ran fine + exit 0 + nothing to check ⇒ honestly can't confirm."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=True, ran_probe_errors=[], last_exit=0,
        delivered_declared=False,
    )
    assert cls.outcome == dm.OUTCOME_UNMEASURABLE
    assert dm.UNDECLARED_EVIDENCE_NOTE in cls.probe_errors


def test_classify_no_fire_without_declared_evidence_is_did_not_run():
    """did_not_run needs no delivery evidence — undeclared-evidence apps
    must still get honest no-fire misses (previously unmeasurable)."""
    cls = dm.classify_window(
        window=WINDOW, delivered_at=None, delivered_probe_errors=[],
        ran=False, ran_probe_errors=[], loaded=False,
        delivered_declared=False,
    )
    assert cls.outcome == dm.OUTCOME_MISSED
    assert cls.diagnosis == dm.DIAGNOSIS_DID_NOT_RUN
    assert cls.suspected_cause == "not_loaded"


def test_classify_on_time_short_circuits_exit_status():
    """A delivered window stays on_time even if a LATER run crashed and
    left a non-zero LastExitStatus behind."""
    cls = dm.classify_window(
        window=WINDOW,
        delivered_at=datetime(2026, 6, 9, 7, 10, tzinfo=NY),
        delivered_probe_errors=[], ran=True, ran_probe_errors=[],
        last_exit=1,
    )
    assert cls.outcome == dm.OUTCOME_ON_TIME


def test_gather_ran_extracts_last_exit(tmp_path):
    """launchctl's LastExitStatus rides along with the ran evidence."""
    action = _action(tmp_path)
    _touch_wrapper_log(tmp_path, "test-action", datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    probes = _probes({
        "launchctl list": (0, '{\n\t"LastExitStatus" = 1;\n}', ""),
    })
    ran, loaded, last_exit, errors = dm.gather_ran(action, WINDOW, probes)
    assert ran is True
    assert last_exit == 1
    assert errors == []


def test_gather_ran_ignores_last_exit_while_job_is_running(tmp_path):
    """A live PID means LastExitStatus belongs to a PREVIOUS run — never
    attribute it to the run in flight (false script_error misses)."""
    action = _action(tmp_path)
    _touch_wrapper_log(tmp_path, "test-action", datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    probes = _probes({
        "launchctl list": (0, '{\n\t"LastExitStatus" = 256;\n\t"PID" = 9;\n}', ""),
    })
    ran, loaded, last_exit, errors = dm.gather_ran(action, WINDOW, probes)
    assert ran is True
    assert last_exit is None


def test_launchctl_job_normalizes_wait_status_encoding():
    """launchctl prints the raw wait status: exit(1) shows as 256. The
    probe hands callers the script's actual exit code (pod ground truth:
    the broken ea-pack jobs showed LastExitStatus = 256)."""
    p = dm.Probes(runner=FakeRunner({
        "launchctl list": (0, '{\n\t"LastExitStatus" = 256;\n}', ""),
    }))
    job, err = p.launchctl_job("ai.evolve.testbot.test-app")
    assert err is None
    assert job == {"loaded": True, "last_exit": 1, "running": False}


def test_gather_delivered_undeclared_is_flag_not_probe_error(tmp_path):
    action = _action(tmp_path, contract=_contract(delivered=None, source="derived"))
    delivered_at, errors, use_ran, declared = dm.gather_delivered(
        action, WINDOW, _probes(),
    )
    assert delivered_at is None
    assert errors == []
    assert use_ran is False
    assert declared is False


# ─────────────────────────────────────────────────────────────────────────────
# Window math
# ─────────────────────────────────────────────────────────────────────────────


def test_latest_calendar_fire_today_when_past():
    now = datetime(2026, 6, 9, 8, 0, tzinfo=NY)
    fire = dm.latest_calendar_fire({"Hour": 7, "Minute": 0}, now)
    assert fire == datetime(2026, 6, 9, 7, 0, tzinfo=NY)


def test_latest_calendar_fire_yesterday_when_not_yet_due():
    now = datetime(2026, 6, 9, 6, 0, tzinfo=NY)
    fire = dm.latest_calendar_fire({"Hour": 7, "Minute": 0}, now)
    assert fire == datetime(2026, 6, 8, 7, 0, tzinfo=NY)


def test_latest_calendar_fire_weekday_walks_back():
    # launchd Weekday 0 == Sunday; 2026-06-09 is a Tuesday.
    now = datetime(2026, 6, 9, 12, 0, tzinfo=NY)
    fire = dm.latest_calendar_fire({"Hour": 9, "Minute": 30, "Weekday": 0}, now)
    assert fire == datetime(2026, 6, 7, 9, 30, tzinfo=NY)
    assert fire.weekday() == 6  # Python Sunday


def test_latest_calendar_fire_rejects_garbage():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=NY)
    assert dm.latest_calendar_fire({"Hour": "seven"}, now) is None
    assert dm.latest_calendar_fire({"Hour": 99}, now) is None


def test_job_timezone_falls_back_to_host_when_absent():
    fallback = timezone.utc
    tz, err = dm.job_timezone(None, fallback)
    assert tz is fallback and err is None


def test_job_timezone_invalid_is_an_error_not_a_guess():
    tz, err = dm.job_timezone("Mars/Olympus_Mons", timezone.utc)
    assert tz is None
    assert err and "Mars/Olympus_Mons" in err


def test_window_straddles_dst_spring_forward():
    # US DST began 2026-03-08 02:00 in America/New_York.
    w = dm.Window(
        start=datetime(2026, 3, 8, 1, 30, tzinfo=NY),
        end=datetime(2026, 3, 8, 3, 30, tzinfo=NY),
    )
    assert dm.window_straddles_dst(w) is True
    assert dm.window_straddles_dst(WINDOW) is False


def test_due_window_calendar_not_yet_elapsed_returns_none():
    action = _action(Path("/nonexistent"))
    # 07:10 NY — inside the open window; nothing due yet, no errors.
    now = datetime(2026, 6, 9, 11, 10, 0, tzinfo=timezone.utc)
    window, is_interval, errs = dm._due_window(action, now, _probes())
    assert window is None and errs == [] and is_interval is False


def test_due_window_reads_tz_from_plist():
    action = _action(Path("/nonexistent"))
    window, _, errs = dm._due_window(action, NOW, _probes())
    assert errs == []
    assert window is not None
    assert window.start == datetime(2026, 6, 9, 7, 0, tzinfo=NY)
    assert window.end == datetime(2026, 6, 9, 7, 30, tzinfo=NY)


def test_due_window_invalid_plist_tz_is_unmeasurable(_plist_dir):
    bad = _PLIST_WITH_TZ.replace("America/New_York", "Not/AZone")
    (_plist_dir / "ai.evolve.testbot.test-app.plist").write_text(bad)
    action = _action(Path("/nonexistent"))
    window, _, errs = dm._due_window(action, NOW, _probes())
    assert window is None
    assert errs and "Not/AZone" in errs[0]


def test_due_window_missing_plist_falls_back_to_host_tz(_plist_dir):
    """A bootout'd / absent plist must not make the window ambiguous —
    §6.3: TZ falls back to host TZ; the load-state probe owns the rest."""
    (_plist_dir / "ai.evolve.testbot.test-app.plist").unlink()
    action = _action(Path("/nonexistent"))
    _window, _, errs = dm._due_window(action, NOW, _probes())
    assert errs == []


def test_due_window_interval_shape():
    action = _action(Path("/nonexistent"), schedule={"every_minutes": 15},
                     contract=_contract(window_minutes=15))
    window, is_interval, errs = dm._due_window(action, NOW, _probes())
    assert is_interval is True and errs == []
    assert window is not None
    assert window.end - window.start == timedelta(minutes=30)  # interval + grace


# ─────────────────────────────────────────────────────────────────────────────
# Effective contract (§5 — B layered over A)
# ─────────────────────────────────────────────────────────────────────────────


def test_declared_contract_is_authoritative():
    action = {
        "id": "a", "outputs": [],
        "delivery_contract": {
            "user_facing": True, "window_minutes": 45,
            "evidence": {"delivered": {"kind": "run_file", "path": "m/{date}.json"}},
            "heal": "rerun",
        },
    }
    c = dm.effective_contract({}, action)
    assert c.source == "declared"
    assert c.user_facing is True and c.window_minutes == 45 and c.heal == "rerun"
    assert c.delivered == {"kind": "run_file", "path": "m/{date}.json"}


def test_malformed_contract_falls_back_to_derived_defaults():
    action = {
        "id": "a",
        "outputs": [{"kind": "session_message", "channel": "primary"}],
        "delivery_contract": {"heal": "yolo", "window_minutes": -3},
    }
    c = dm.effective_contract({}, action)
    assert c.source == "derived"
    assert c.heal == "none"  # never force-run on a contract we can't trust
    assert c.window_minutes == dm.DEFAULT_WINDOW_MINUTES


def test_derived_user_facing_iff_outputs_declare_a_channel():
    msg = {"id": "a", "outputs": [{"kind": "session_message", "channel": "primary"}]}
    data = {"id": "b", "outputs": [{"kind": "data_file", "path": "x.md"}]}
    none = {"id": "c"}
    assert dm.effective_contract({}, msg).user_facing is True
    assert dm.effective_contract({}, data).user_facing is False
    assert dm.effective_contract({}, none).user_facing is False


def test_derived_delivered_evidence_prefers_sent_prefix():
    manifest = {"interface_contract": {"signal_prefixes": [
        "BRIEFING_FAILED:", "BRIEFING_SENT:", "BRIEFING_SKIPPED:",
    ]}}
    c = dm.effective_contract(manifest, {"id": "a", "outputs": []})
    assert c.delivered == {"kind": "signal_line", "pattern": "BRIEFING_SENT:", "log": None}


def test_derived_delivered_evidence_none_when_no_prefixes():
    c = dm.effective_contract({}, {"id": "a", "outputs": []})
    assert c.delivered is None


# ─────────────────────────────────────────────────────────────────────────────
# Probes (runner seam)
# ─────────────────────────────────────────────────────────────────────────────


def test_probe_launchctl_loaded_parses_exit_status():
    p = dm.Probes(runner=FakeRunner({"launchctl": _LAUNCHCTL_LOADED}))
    job, err = p.launchctl_job("ai.evolve.testbot.test-app")
    assert err is None
    # _LAUNCHCTL_LOADED carries a PID line: the job is mid-run, so
    # running=True and LastExitStatus (a previous run's) still parses.
    assert job == {"loaded": True, "last_exit": 0, "running": True}


def test_probe_launchctl_not_found_is_not_loaded_not_an_error():
    p = dm.Probes(runner=FakeRunner({"launchctl": _LAUNCHCTL_NOT_FOUND}))
    job, err = p.launchctl_job("ai.evolve.testbot.test-app")
    assert err is None
    assert job == {"loaded": False, "last_exit": None, "running": False}


def test_probe_launchctl_sudo_denied_is_a_probe_error():
    p = dm.Probes(runner=FakeRunner({"launchctl": _SUDO_DENIED}))
    job, err = p.launchctl_job("ai.evolve.testbot.test-app")
    assert job is None
    assert err and "password" in err


def test_probe_read_text_sudo_fallback_under_unreachable_parent(tmp_path):
    """exists() lies under 0700 parents — when even the parent can't be
    statted, the sudo grant settles whether the file is there."""
    runner = FakeRunner({"/bin/cat": (0, "secret", "")})
    p = dm.Probes(runner=runner)
    text, err = p.read_text(tmp_path / "noparent" / "absent.json")
    assert (text, err) == ("secret", None)
    assert runner.calls and runner.calls[0][:3] == ["sudo", "-n", "/bin/cat"]


def test_probe_read_text_missing_with_readable_parent_skips_sudo(tmp_path):
    runner = FakeRunner()
    p = dm.Probes(runner=runner)
    text, err = p.read_text(tmp_path / "absent.json")
    assert (text, err) == (None, None)
    assert runner.calls == []  # provable absence — no sudo round-trip


def test_probe_read_text_sudo_denied_is_error(tmp_path):
    p = dm.Probes(runner=FakeRunner({"/bin/cat": _SUDO_DENIED}))
    text, err = p.read_text(tmp_path / "noparent" / "absent.json")
    assert text is None
    assert err and "rc=1" in err


# ─────────────────────────────────────────────────────────────────────────────
# check_action state machine
# ─────────────────────────────────────────────────────────────────────────────


def _checked(action, *, shared_dir, now=NOW, state=None, probes=None):
    state = state if state is not None else {}
    return dm.check_action(
        action, shared_dir=shared_dir, now=now, state=state,
        probes=probes or _probes({"launchctl": _LAUNCHCTL_LOADED}),
    ), state


def test_on_time_window_no_signal_one_ledger_row(tmp_path):
    ws = tmp_path / "ws"
    _write_run_file(ws, "2026-06-09T07:10:00-04:00")
    action = _action(ws)
    result, state = _checked(action, shared_dir=tmp_path)
    assert result.specs == [] and result.kept == set()
    assert [r["outcome"] for r in result.ledger_rows] == [dm.OUTCOME_ON_TIME]


def test_check_action_exit1_undeclared_evidence_is_missed_not_unmeasurable(tmp_path):
    """The live OC-2026.6 pod shape: contract carries no delivery
    evidence (pre-v23 instances), the wrapper log shows a fire in the
    window, launchctl reports LastExitStatus 1. Must classify as a
    ran_undelivered miss with the exit status on the Signal — the
    8-days-masked-as-unmeasurable regression."""
    ws = tmp_path / "ws"
    action = _action(
        ws,
        contract=_contract(delivered=None, source="derived"),
    )
    _touch_wrapper_log(ws, "test-action", datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    probes = _probes({
        "launchctl list": (0, '{\n\t"LastExitStatus" = 1;\n}', ""),
    })
    result, _state = _checked(action, shared_dir=tmp_path, probes=probes)
    assert [r["outcome"] for r in result.ledger_rows] == [dm.OUTCOME_MISSED]
    assert result.ledger_rows[0]["diagnosis"] == dm.DIAGNOSIS_RAN_UNDELIVERED
    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec["type"] == dm.TYPE_MISSED
    assert spec["details"]["suspected_cause"] == "script_error"
    assert spec["details"]["last_exit"] == 1


def test_check_action_exit0_undeclared_evidence_stays_unmeasurable(tmp_path):
    """Healthy run + nothing to check keeps the honest 'can't confirm'."""
    ws = tmp_path / "ws"
    action = _action(
        ws,
        contract=_contract(delivered=None, source="derived"),
    )
    _touch_wrapper_log(ws, "test-action", datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    result, _state = _checked(action, shared_dir=tmp_path)
    assert [r["outcome"] for r in result.ledger_rows] == [dm.OUTCOME_UNMEASURABLE]
    assert len(result.specs) == 1
    assert result.specs[0]["type"] == dm.TYPE_UNMEASURABLE


def test_window_classified_once_across_ticks(tmp_path):
    ws = tmp_path / "ws"
    _write_run_file(ws, "2026-06-09T07:10:00-04:00")
    action = _action(ws)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state)
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state)
    assert len(r1.ledger_rows) == 1 and r2.ledger_rows == []


def test_missed_fires_warn_then_escalates_to_alert(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state)
    assert len(r1.specs) == 1
    assert r1.specs[0]["severity"] == "warn"
    assert r1.specs[0]["details"]["diagnosis"] == dm.DIAGNOSIS_DID_NOT_RUN
    # Next day's window also missed → same signature, alert.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(days=1),
                     state=state)
    assert len(r2.specs) == 1
    assert r2.specs[0]["severity"] == "alert"
    assert r2.specs[0]["signature"] == r1.specs[0]["signature"]


def test_missed_signal_kept_alive_between_windows(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state)
    sig = r1.specs[0]["signature"]
    # 5 minutes later: same window already classified; no new spec, but
    # the signature must stay in kept or sweep_resolve closes it early.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state)
    assert r2.specs == []
    assert sig in r2.kept


def test_late_delivery_writes_recovery_then_releases_signature(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state)
    assert r1.specs[0]["details"]["recovery"] is None
    # The briefing lands at 08:00 — after the window.
    _write_run_file(ws, "2026-06-09T08:00:00-04:00")
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state)
    assert len(r2.specs) == 1
    recovery = r2.specs[0]["details"]["recovery"]
    assert recovery and recovery["delivered_at"].startswith("2026-06-09T08:00")
    # Signature released → run()'s sweep_resolve archives it.
    assert r2.kept == set()
    assert [r["outcome"] for r in r2.ledger_rows] == [dm.OUTCOME_LATE]
    # Counter resets: the next miss is a fresh first miss.
    assert state["actions"][action.key]["consecutive_misses"] == 0


def test_unmeasurable_escalates_to_warn_after_three_windows(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws)
    probes = _probes({"launchctl": _SUDO_DENIED})
    state: dict = {}
    sevs = []
    for day in range(3):
        r, _ = _checked(action, shared_dir=tmp_path,
                        now=NOW + timedelta(days=day), state=state, probes=probes)
        assert len(r.specs) == 1
        assert r.specs[0]["type"] == dm.TYPE_UNMEASURABLE
        sevs.append(r.specs[0]["severity"])
    assert sevs == ["info", "info", "warn"]


def test_measurable_window_resets_unmeasurable_counter(tmp_path):
    ws = tmp_path / "ws"
    _write_run_file(ws, "2026-06-09T07:10:00-04:00")
    action = _action(ws)
    state = {"actions": {action.key: {
        "consecutive_unmeasurable": 2, "active_signal": dm.TYPE_UNMEASURABLE,
        "first_seen_at": "2026-06-01T00:00:00+00:00",
    }}}
    r, state = _checked(action, shared_dir=tmp_path, state=state)
    assert r.specs == [] and r.kept == set()
    assert state["actions"][action.key]["consecutive_unmeasurable"] == 0


def test_first_install_skips_until_first_full_window(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Installed at 07:15 — mid-window. No Signal for today's 07:00 window.
    action = _action(ws, installed_at="2026-06-09T07:15:00-04:00")
    r, state = _checked(action, shared_dir=tmp_path)
    assert r.specs == [] and r.ledger_rows == []
    # Tomorrow's window IS judged.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(days=1),
                     state=state)
    assert len(r2.specs) == 1


def test_never_seen_action_with_no_installed_at_gets_a_grace_window(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws, installed_at=None)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state)
    assert r1.specs == []  # first sighting is the baseline, not a miss
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(days=1),
                     state=state)
    assert len(r2.specs) == 1


def test_operator_disabled_is_not_a_miss(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws, action_state="disabled")
    r, state = _checked(action, shared_dir=tmp_path)
    assert r.specs == []
    assert [row["outcome"] for row in r.ledger_rows] == [dm.OUTCOME_DISABLED]
    # Logged once, not every tick.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state)
    assert r2.ledger_rows == []


def test_interval_misses_held_until_two_consecutive_ticks(tmp_path):
    """§15 OQ1 posture: one missed 15-min tick is noise."""
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(
        ws, schedule={"every_minutes": 15},
        contract=_contract(
            window_minutes=15,
            delivered={"kind": "scheduler_state"},
        ),
    )
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    assert r1.specs == []  # first missed tick: ledger only
    assert [r["outcome"] for r in r1.ledger_rows] == [dm.OUTCOME_MISSED]
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=15),
                     state=state, probes=probes)
    assert len(r2.specs) == 1 and r2.specs[0]["severity"] == "warn"
    r3, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=30),
                     state=state, probes=probes)
    assert r3.specs[0]["severity"] == "alert"


def test_interval_evaluated_once_per_interval_not_per_tick(tmp_path):
    ws = tmp_path / "ws"
    action = _action(
        ws, schedule={"every_minutes": 15},
        contract=_contract(window_minutes=15, delivered={"kind": "scheduler_state"}),
    )
    _touch_wrapper_log(ws, action.action_id, NOW - timedelta(minutes=3))
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    assert [r["outcome"] for r in r1.ledger_rows] == [dm.OUTCOME_ON_TIME]
    # 5 minutes later — same interval, no second ledger row.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state, probes=probes)
    assert r2.ledger_rows == []


def test_ran_undelivered_via_fresh_wrapper_log(tmp_path):
    ws = tmp_path / "ws"
    action = _action(ws)
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    r, _ = _checked(action, shared_dir=tmp_path)
    assert len(r.specs) == 1
    assert r.specs[0]["details"]["diagnosis"] == dm.DIAGNOSIS_RAN_UNDELIVERED


def test_ran_undelivered_links_active_gateway_down_signal(tmp_path):
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    gw = signals_store.observe(
        shared,
        signature="heal:gateway_down:testbot",
        producer="heal", type="gateway_down", flavor="activity",
        severity="alert", scope="bot", bot_id="testbot",
        title="gateway down",
    )
    action = _action(ws)
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    r, _ = _checked(action, shared_dir=shared)
    assert r.specs[0]["caused_by_signal_id"] == gw.id
    assert r.specs[0]["details"]["suspected_cause"] == "gateway_down"


def _observe_host_slept(shared: Path, *, slept: datetime, woke: datetime | None):
    details = {"last_sleep_at": slept.timestamp()}
    if woke is not None:
        details["last_wake_at"] = woke.timestamp()
    return signals_store.observe(
        shared,
        signature="host_health:host_slept:testhost",
        producer="host_health", type="host_slept", flavor="maintenance",
        severity="alert", scope="host", title="Host slept",
        details=details,
    )


def test_host_asleep_defers_classification_until_wake_plus_grace(tmp_path):
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    ws.mkdir()
    # Slept through the whole window; woke at 08:00 NY — five minutes ago.
    _observe_host_slept(
        shared,
        slept=datetime(2026, 6, 9, 6, 0, tzinfo=NY),
        woke=datetime(2026, 6, 9, 8, 0, tzinfo=NY),
    )
    action = _action(ws)
    r, _ = _checked(action, shared_dir=shared)  # NOW = 08:05 NY
    assert r.specs == [] and r.ledger_rows == []  # launchd's queued run gets its chance


def test_host_asleep_past_allowance_fires_with_cause(tmp_path):
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    ws.mkdir()
    _observe_host_slept(
        shared,
        slept=datetime(2026, 6, 9, 6, 50, tzinfo=NY),
        woke=datetime(2026, 6, 9, 7, 32, tzinfo=NY),
    )
    action = _action(ws)
    # 08:05 NY: wake (07:32) + 30 min allowance = 08:02 — passed.
    r, _ = _checked(action, shared_dir=shared)
    assert len(r.specs) == 1
    assert r.specs[0]["details"]["suspected_cause"] == "host_asleep"
    assert r.specs[0]["caused_by_signal_id"]


def test_no_delivery_evidence_declared_goes_unmeasurable_after_run(tmp_path):
    """A user-facing app with no run_file/signal_line anywhere: 'ran' must
    not be coerced into 'delivered' (the anti-fake-green rule)."""
    ws = tmp_path / "ws"
    action = _action(ws, contract=_contract(delivered=None))
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    r, _ = _checked(action, shared_dir=tmp_path)
    assert len(r.specs) == 1
    assert r.specs[0]["type"] == dm.TYPE_UNMEASURABLE


# ─────────────────────────────────────────────────────────────────────────────
# Monitored-set construction
# ─────────────────────────────────────────────────────────────────────────────


def _install_manifest(home: Path, manifest: dict) -> Path:
    mdir = home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    path = mdir / f"{manifest['id']}.json"
    path.write_text(json.dumps(manifest))
    return path


_MANIFEST = {
    "id": "test-app",
    "display_name": "Test App",
    "status": "active",
    "interface_contract": {"signal_prefixes": ["SENT:"]},
    "scheduled_actions": [
        {
            "id": "calendar-action",
            "mechanism": "launchd",
            "installed_at": "2026-06-01T00:00:00-04:00",
            "install": {
                "plist_label": "ai.evolve.${bot_id}.test-app",
                "schedule": {"cron": {"Hour": 7, "Minute": 0}},
            },
            "outputs": [{"kind": "session_message", "channel": "primary"}],
            "delivery_contract": {
                "user_facing": True, "window_minutes": 30,
                "evidence": {"delivered": {
                    "kind": "run_file", "path": "memory/briefing-runs/{date}.json",
                }},
                "heal": "rerun",
            },
        },
        {
            "id": "greet-action",
            "mechanism": "oc_session_instruction",
            "outputs": [{"kind": "session_message", "channel": "primary"}],
        },
        {
            "id": "cache-action",
            "mechanism": "launchd",
            "install": {"schedule": {"cron": {"Hour": 3, "Minute": 30}}},
            "outputs": [{"kind": "data_file", "path": "cache.md"}],
        },
    ],
}


def test_build_monitored_set_buckets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_manifest(home, _MANIFEST)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    network = {"bots": {"testbot": {}}}
    monitored, coverage, ws_errors = dm.build_monitored_set(network, _probes())
    assert ws_errors == {}
    assert [a.action_id for a in monitored] == ["calendar-action"]
    assert monitored[0].label == "ai.evolve.home.test-app"  # ${bot_id} → account name
    assert monitored[0].contract.heal == "rerun"
    # Session-instruction action is visibly unmonitorable; the cache
    # action (not user-facing) is silently out of scope (§2).
    assert [(c["action_id"], c["outcome"]) for c in coverage] == [
        ("greet-action", dm.OUTCOME_UNMONITORABLE),
    ]


def test_build_monitored_set_reports_unlistable_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = home / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (ws / "manifests").write_text("not a dir")  # iterdir → NotADirectoryError
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    monitored, coverage, ws_errors = dm.build_monitored_set(
        {"bots": {"testbot": {}}}, _probes(),
    )
    assert monitored == [] and "testbot" in ws_errors


# ─────────────────────────────────────────────────────────────────────────────
# run() — Signals + ledger + sweep against a real tmp Signal store
# ─────────────────────────────────────────────────────────────────────────────


def test_run_end_to_end_miss_then_recovery_sweeps(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    _install_manifest(home, _MANIFEST)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    network = {"bots": {"testbot": {}}}
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})

    kept, fired, resolved = dm.run(shared, network, probes=probes, now=NOW)
    assert fired == 1 and resolved == 0
    firing = list(signals_store.iter_active(shared, producer=dm.PRODUCER))
    assert len(firing) == 1
    assert firing[0].type == dm.TYPE_MISSED
    # The fixture app declares heal:"rerun" but is not the soak canary —
    # the holdback is visible in details.heal, never silent (§13.3).
    assert firing[0].details["heal"] == {
        "attempted": False, "action": "none", "result": "canary_holdback",
    }

    # Ledger: one unmonitorable coverage row + one missed window row.
    ledger_file = shared / "delivery_monitor" / "ledger" / f"{NOW:%Y-%m-%d}.jsonl"
    rows = [json.loads(line) for line in ledger_file.read_text().splitlines()]
    assert sorted(r["outcome"] for r in rows) == [
        dm.OUTCOME_MISSED, dm.OUTCOME_UNMONITORABLE,
    ]

    # The briefing lands late → recovery recorded, Signal sweep-resolved.
    ws = home / ".openclaw" / "workspace"
    _write_run_file(ws, "2026-06-09T08:10:00-04:00")
    kept2, fired2, resolved2 = dm.run(
        shared, network, probes=probes, now=NOW + timedelta(minutes=10),
    )
    assert resolved2 == 1
    assert list(signals_store.iter_active(shared, producer=dm.PRODUCER)) == []
    archived = [
        s for s in signals_store.iter_signals(shared, subdirs=("archived",))
        if s.producer == dm.PRODUCER
    ]
    assert len(archived) == 1
    assert archived[0].details["recovery"]["delivered_at"].startswith("2026-06-09T08:10")
    # The fire-time heal verdict survives onto the resolved Signal — the
    # holdback stays on the record instead of degrading to "not_attempted".
    assert archived[0].details["heal"]["result"] == "canary_holdback"


def test_run_coverage_row_logged_once(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    ws = home / ".openclaw" / "workspace"
    _install_manifest(home, _MANIFEST)
    _write_run_file(ws, "2026-06-09T07:10:00-04:00")
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    network = {"bots": {"testbot": {}}}
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})

    dm.run(shared, network, probes=probes, now=NOW)
    dm.run(shared, network, probes=probes, now=NOW + timedelta(minutes=5))
    ledger_file = shared / "delivery_monitor" / "ledger" / f"{NOW:%Y-%m-%d}.jsonl"
    rows = [json.loads(line) for line in ledger_file.read_text().splitlines()]
    unmonitorable = [r for r in rows if r["outcome"] == dm.OUTCOME_UNMONITORABLE]
    assert len(unmonitorable) == 1


def test_run_workspace_error_fires_per_bot_unmeasurable(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    ws = home / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (ws / "manifests").write_text("not a dir")
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    kept, fired, _ = dm.run(shared, {"bots": {"testbot": {}}}, probes=_probes(), now=NOW)
    assert fired == 1
    firing = list(signals_store.iter_active(shared, producer=dm.PRODUCER))
    assert firing[0].type == dm.TYPE_UNMEASURABLE
    assert firing[0].bot_id == "testbot"


def test_run_blind_workspace_does_not_sweep_existing_misses(tmp_path, monkeypatch):
    """If the monitor goes blind on a bot's workspace, its existing missed
    Signals are UNKNOWN, not cleared — sweep_resolve must not archive them."""
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    _install_manifest(home, _MANIFEST)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    network = {"bots": {"testbot": {}}}
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})

    dm.run(shared, network, probes=probes, now=NOW)  # fires the miss
    assert any(
        s.type == dm.TYPE_MISSED
        for s in signals_store.iter_active(shared, producer=dm.PRODUCER)
    )

    # Break the workspace listing for the next tick.
    mdir = home / ".openclaw" / "workspace" / "manifests"
    import shutil
    shutil.rmtree(mdir)
    mdir.parent.joinpath("manifests").write_text("not a dir")
    _, _, resolved = dm.run(
        shared, network, probes=probes, now=NOW + timedelta(minutes=5),
    )
    types = {s.type for s in signals_store.iter_active(shared, producer=dm.PRODUCER)}
    assert dm.TYPE_MISSED in types          # the miss survives the blind tick
    assert dm.TYPE_UNMEASURABLE in types    # and the blindness itself fires


def test_run_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    shared = tmp_path / "shared"
    home = tmp_path / "home"
    _install_manifest(home, _MANIFEST)
    monkeypatch.setattr(dm, "bot_home", lambda bot_id, network=None: home)
    probes = _probes({"launchctl": _LAUNCHCTL_LOADED})
    dm.run(shared, {"bots": {"testbot": {}}}, probes=probes, now=NOW, dry_run=True)
    assert list(signals_store.iter_active(shared, producer=dm.PRODUCER)) == []
    assert not (shared / "delivery_monitor" / "state.json").exists()
    assert not (shared / "delivery_monitor" / "ledger").exists()
    assert "would_observe" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# Heal (§8) — gating, paths, one-attempt, heal_wait, deferred rerun
# ─────────────────────────────────────────────────────────────────────────────

CANARY = "app_morning_briefing"

_PRINT_LOADED = (0, 'system/ai.evolve.testbot.test-app = {\n\tstate = waiting\n}', "")
_PRINT_NOT_FOUND = (
    64, "", 'Could not find service "ai.evolve.testbot.test-app" in domain for system',
)
_CMD_OK = (0, "", "")
_PLUTIL_OK = (0, "/Library/LaunchDaemons/x.plist: OK", "")
_PLUTIL_BAD = (1, "", "x.plist: Unexpected character at line 1")

_HEAL_OK_RESPONSES = {
    "launchctl list": _LAUNCHCTL_LOADED,
    "launchctl print": _PRINT_LOADED,
    "launchctl kickstart": _CMD_OK,
    "launchctl bootstrap": _CMD_OK,
    "plutil": _PLUTIL_OK,
}


def _canary_action(workspace: Path, **over) -> dm.MonitoredAction:
    over.setdefault("app_id", CANARY)
    over.setdefault("contract", _contract(heal="rerun"))
    return _action(workspace, **over)


def _heal_verb(argv: list[str]) -> str | None:
    """The heal-command verb of one runner call, or None.

    Matches exact argv elements — tmp_path test-dir names contain words
    like "bootstrap"/"kickstart", so substring matching on the joined
    command line would catch unrelated ``sudo cat`` calls.
    """
    if argv and argv[0] == "/usr/bin/plutil":
        return "plutil"
    if "/bin/launchctl" in argv:
        for verb in ("print", "kickstart", "bootstrap", "list"):
            if verb in argv:
                return verb
    return None


def _calls_with(runner: FakeRunner, verb: str) -> list[list[str]]:
    return [c for c in runner.calls if _heal_verb(c) == verb]


def _heal_cls(**over) -> dm.Classification:
    kwargs = dict(outcome=dm.OUTCOME_MISSED, diagnosis=dm.DIAGNOSIS_DID_NOT_RUN)
    kwargs.update(over)
    return dm.Classification(**kwargs)


def _attempt(action, cls, *, entry=None, probes=None, now=NOW):
    return dm.attempt_heal(
        action, cls, window=WINDOW, entry=entry if entry is not None else {},
        probes=probes or _probes(), now=now,
    )


def test_heal_gate_requires_declared_rerun(tmp_path):
    """heal != "rerun" never reruns — even for the canary app."""
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    action = _canary_action(tmp_path, contract=_contract(heal="none"))
    heal = _attempt(action, _heal_cls(), probes=dm.Probes(runner=runner))
    assert heal == {"attempted": False, "action": "none", "result": "no_heal_declared"}
    assert runner.calls == []


def test_heal_gate_canary_holdback_is_visible_not_silent(tmp_path):
    """A heal:"rerun" app outside HEAL_CANARY_APP_IDS reports the
    holdback (§13.3 soak) — and runs nothing."""
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    action = _action(tmp_path, contract=_contract(heal="rerun"))  # app_id=test-app
    heal = _attempt(action, _heal_cls(), probes=dm.Probes(runner=runner))
    assert heal == {"attempted": False, "action": "none", "result": "canary_holdback"}
    assert runner.calls == []


def test_heal_gate_dst_and_asleep_are_report_only(tmp_path):
    action = _canary_action(tmp_path)
    assert _attempt(action, _heal_cls(suspected_cause="dst"))["result"] == "no_heal_for_dst"
    assert _attempt(
        action, _heal_cls(suspected_cause="host_asleep"),
    )["result"] == "no_heal_while_asleep"


def test_heal_gate_script_error_is_report_only(tmp_path):
    """A kickstart would re-run the same crashing script — the fix is
    the app, not the scheduler (delivery-convention migration §4)."""
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    action = _canary_action(tmp_path)
    heal = _attempt(
        action, _heal_cls(suspected_cause="script_error"),
        probes=dm.Probes(runner=runner),
    )
    assert heal["result"] == "no_heal_for_script_error"
    assert runner.calls == []


def test_heal_oc_cron_mechanism_is_report_only(tmp_path):
    """No forced-run interface on the runtime seam (§15 OQ2)."""
    action = _canary_action(tmp_path, mechanism="openclaw_cron")
    heal = _attempt(action, _heal_cls())
    assert heal["result"] == "no_forced_run_interface"
    assert heal["attempted"] is False


def test_heal_kickstart_loaded_job_no_dash_k(tmp_path):
    """§8 row 1: loaded but didn't fire → one-shot kickstart WITHOUT -k."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    action = _canary_action(ws)
    r, state = _checked(action, shared_dir=tmp_path, probes=dm.Probes(runner=runner))
    assert len(r.specs) == 1
    heal = r.specs[0]["details"]["heal"]
    assert heal["attempted"] is True
    assert heal["action"] == "kickstart"
    assert heal["result"] == "restarted"
    kicks = _calls_with(runner, "kickstart")
    assert kicks == [[
        "sudo", "-n", "/bin/launchctl", "kickstart",
        "system/ai.evolve.testbot.test-app",
    ]]
    assert "-k" not in kicks[0]
    # Restart command success is NOT delivery success: severity stays
    # warn and the copy says Evolve is watching, not that it's fixed.
    assert r.specs[0]["severity"] == "warn"
    assert "watching for the delivery" in r.specs[0]["body"]
    assert [row["healed"] for row in r.ledger_rows] == [True]


def test_heal_no_grant_reports_couldnt_attempt(tmp_path):
    """sudo -n denied on the grant probe ⇒ tri-state report, no silence."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner({
        "launchctl list": _LAUNCHCTL_LOADED,
        "launchctl print": _SUDO_DENIED,
    })
    action = _canary_action(ws)
    r, _ = _checked(action, shared_dir=tmp_path, probes=dm.Probes(runner=runner))
    heal = r.specs[0]["details"]["heal"]
    assert heal["attempted"] is False
    assert heal["result"] == "no_grant"
    assert "password" in heal["error"]
    assert "couldn't attempt an automatic restart" in r.specs[0]["body"]
    assert r.specs[0]["severity"] == "warn"
    assert _calls_with(runner, "kickstart") == []


def test_heal_bootstrap_path_lints_then_bootstraps_then_kickstarts(tmp_path):
    """§8 row 2 — the §13 proof case: plist on disk, label not loaded."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner({
        "launchctl list": _LAUNCHCTL_NOT_FOUND,
        "launchctl print": _PRINT_NOT_FOUND,
        "launchctl kickstart": _CMD_OK,
        "launchctl bootstrap": _CMD_OK,
        "plutil": _PLUTIL_OK,
    })
    action = _canary_action(ws)
    r, _ = _checked(action, shared_dir=tmp_path, probes=dm.Probes(runner=runner))
    heal = r.specs[0]["details"]["heal"]
    assert heal["attempted"] is True
    assert heal["action"] == "bootstrap+kickstart"
    assert heal["result"] == "restarted"
    ordered = [
        v for v in map(_heal_verb, runner.calls)
        if v in ("plutil", "bootstrap", "kickstart")
    ]
    assert ordered == ["plutil", "bootstrap", "kickstart"]


def test_heal_plist_missing_escalates_to_alert(tmp_path, _plist_dir):
    (_plist_dir / "ai.evolve.testbot.test-app.plist").unlink()
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner({
        "launchctl list": _LAUNCHCTL_NOT_FOUND,
        "launchctl print": _PRINT_NOT_FOUND,
    })
    action = _canary_action(ws)
    r, _ = _checked(action, shared_dir=tmp_path, probes=dm.Probes(runner=runner))
    heal = r.specs[0]["details"]["heal"]
    assert heal["result"] == "plist_missing"
    assert r.specs[0]["severity"] == "alert"
    assert "didn't work" in r.specs[0]["body"]


def test_heal_plist_invalid_is_not_bootstrapped(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner({
        "launchctl list": _LAUNCHCTL_NOT_FOUND,
        "launchctl print": _PRINT_NOT_FOUND,
        "plutil": _PLUTIL_BAD,
    })
    action = _canary_action(ws)
    r, _ = _checked(action, shared_dir=tmp_path, probes=dm.Probes(runner=runner))
    assert r.specs[0]["details"]["heal"]["result"] == "plist_invalid"
    assert r.specs[0]["severity"] == "alert"
    assert _calls_with(runner, "bootstrap") == []


def test_heal_one_attempt_per_window_across_restarts(tmp_path):
    """§8: one attempt per missed window, enforced through a state.json
    save/load cycle (daemon restart between ticks)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    state: dict = {}
    _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    assert len(_calls_with(runner, "kickstart")) == 1
    dm.save_state(tmp_path, state)
    reloaded = dm.load_state(tmp_path)
    _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
             state=reloaded, probes=probes)
    assert len(_calls_with(runner, "kickstart")) == 1  # still one


def test_heal_wait_quiet_before_expiry_then_escalates_once(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    state: dict = {}
    r1, _ = _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    sig = r1.specs[0]["signature"]
    # +5 min: inside heal_wait — no escalation, signature kept.
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state, probes=probes)
    assert r2.specs == [] and sig in r2.kept
    # +11 min: wait expired, still no delivery → ONE honest 🔴 escalation.
    r3, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=11),
                     state=state, probes=probes)
    assert len(r3.specs) == 1
    assert r3.specs[0]["severity"] == "alert"
    assert r3.specs[0]["title"] == "Test App didn't arrive"
    assert "an automatic restart didn't fix it" in r3.specs[0]["body"]
    assert r3.specs[0]["details"]["heal"]["result"] == "restart_ineffective"
    assert sig in r3.kept
    # +16 min: already escalated — no re-page, condition stays kept.
    r4, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=16),
                     state=state, probes=probes)
    assert r4.specs == [] and sig in r4.kept


def test_recovery_after_heal_says_restarted_and_delivered(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    state: dict = {}
    _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    _write_run_file(ws, "2026-06-09T08:00:00-04:00")
    r2, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=5),
                     state=state, probes=probes)
    recovery = r2.specs[0]["details"]["recovery"]
    assert recovery["summary"] == "Evolve restarted it, and it was delivered at 08:00."
    assert recovery["healed"] is True
    assert [row["healed"] for row in r2.ledger_rows] == [True]
    assert [row["outcome"] for row in r2.ledger_rows] == [dm.OUTCOME_LATE]
    assert r2.kept == set()  # released → sweep_resolve archives it


def test_recovery_after_ineffective_heal_does_not_claim_credit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    state: dict = {}
    _checked(action, shared_dir=tmp_path, state=state, probes=probes)
    _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=11),
             state=state, probes=probes)  # escalates: restart_ineffective
    _write_run_file(ws, "2026-06-09T08:30:00-04:00")
    r3, _ = _checked(action, shared_dir=tmp_path, now=NOW + timedelta(minutes=30),
                     state=state, probes=probes)
    recovery = r3.specs[0]["details"]["recovery"]
    assert recovery["summary"] == "It eventually arrived at 08:30."


def test_recovery_without_heal_names_sleep_cause(tmp_path):
    """Un-healed late delivery after a host-asleep window: the message
    blames the sleep, not the app (§6.3)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    action = _action(ws)  # heal=none
    state = {"actions": {action.key: {
        "first_seen_at": "2026-06-01T00:00:00+00:00",
        "last_window_end": "2026-06-09T07:30:00-04:00",
        "consecutive_misses": 1,
        "active_signal": dm.TYPE_MISSED,
        "pending_miss": {
            "window_start": "2026-06-09T07:00:00-04:00",
            "window_end": "2026-06-09T07:30:00-04:00",
            "diagnosis": dm.DIAGNOSIS_DID_NOT_RUN,
            "suspected_cause": "host_asleep",
        },
    }}}
    _write_run_file(ws, "2026-06-09T08:12:00-04:00")
    r, _ = _checked(action, shared_dir=tmp_path, state=state)
    summary = r.specs[0]["details"]["recovery"]["summary"]
    assert summary == (
        "The computer was asleep during the window; it was delivered at 08:12."
    )
    assert r.specs[0]["details"]["recovery"]["healed"] is False


def test_deferred_rerun_waits_for_gateway_then_fires_once(tmp_path):
    """§6.3/§9.2: ran_undelivered + gateway down ⇒ defer; the rerun fires
    once the gateway Signal resolves; recovery copy names the retry."""
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    gw = signals_store.observe(
        shared,
        signature="heal:gateway_down:testbot",
        producer="heal", type="gateway_down", flavor="activity",
        severity="alert", scope="bot", bot_id="testbot",
        title="gateway down",
    )
    runner = FakeRunner(_HEAL_OK_RESPONSES)
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    state: dict = {}
    r1, _ = _checked(action, shared_dir=shared, state=state, probes=probes)
    heal = r1.specs[0]["details"]["heal"]
    assert heal == {
        "attempted": False, "action": "deferred_rerun",
        "result": "waiting_for_gateway",
    }
    assert "you'll get a follow-up either way" in r1.specs[0]["body"]
    assert _calls_with(runner, "kickstart") == []

    # Gateway still down 5 min later — keep waiting, no rerun.
    r2, _ = _checked(action, shared_dir=shared, now=NOW + timedelta(minutes=5),
                     state=state, probes=probes)
    assert _calls_with(runner, "kickstart") == []
    assert r1.specs[0]["signature"] in r2.kept

    # Gateway recovers → the single deferred rerun fires.
    signals_store.apply_transition(gw, "resolved", shared, actor="test", reason="t")
    r3, _ = _checked(action, shared_dir=shared, now=NOW + timedelta(minutes=10),
                     state=state, probes=probes)
    assert len(_calls_with(runner, "kickstart")) == 1
    assert r3.specs[0]["details"]["heal"]["result"] == "restarted"
    assert r3.specs[0]["details"]["heal"]["deferred"] is True

    # Delivery lands → 🟢 recovery names the retry (the promised follow-up).
    _write_run_file(ws, "2026-06-09T08:20:00-04:00")
    r4, _ = _checked(action, shared_dir=shared, now=NOW + timedelta(minutes=15),
                     state=state, probes=probes)
    assert r4.specs[0]["details"]["recovery"]["summary"] == (
        "Evolve retried it after its messaging connection came back, "
        "and it was delivered at 08:20."
    )
    assert len(_calls_with(runner, "kickstart")) == 1  # still the one attempt


def test_deferred_rerun_failure_escalates_immediately(tmp_path):
    """If the promised retry can't even run, the 🔴 follow-up fires right
    away — the §9.2 promise must never end in silence."""
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    gw = signals_store.observe(
        shared,
        signature="heal:gateway_down:testbot",
        producer="heal", type="gateway_down", flavor="activity",
        severity="alert", scope="bot", bot_id="testbot",
        title="gateway down",
    )
    runner = FakeRunner({
        "launchctl list": _LAUNCHCTL_LOADED,
        "launchctl print": _SUDO_DENIED,
    })
    probes = dm.Probes(runner=runner)
    action = _canary_action(ws)
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    state: dict = {}
    _checked(action, shared_dir=shared, state=state, probes=probes)
    signals_store.apply_transition(gw, "resolved", shared, actor="test", reason="t")
    r2, _ = _checked(action, shared_dir=shared, now=NOW + timedelta(minutes=10),
                     state=state, probes=probes)
    assert len(r2.specs) == 1
    assert r2.specs[0]["severity"] == "alert"
    assert "couldn't be attempted" in r2.specs[0]["body"]


def test_gateway_down_detection_only_makes_no_retry_promise(tmp_path):
    """A non-healing app must not promise a follow-up it won't perform."""
    shared = tmp_path / "shared"
    ws = tmp_path / "ws"
    signals_store.observe(
        shared,
        signature="heal:gateway_down:testbot",
        producer="heal", type="gateway_down", flavor="activity",
        severity="alert", scope="bot", bot_id="testbot",
        title="gateway down",
    )
    action = _action(ws)  # heal=none, not the canary
    _touch_wrapper_log(ws, action.action_id, datetime(2026, 6, 9, 7, 1, tzinfo=NY))
    r, _ = _checked(action, shared_dir=shared)
    assert "messaging connection was down" in r.specs[0]["body"]
    assert "follow-up" not in r.specs[0]["body"]


# ─────────────────────────────────────────────────────────────────────────────
# Pod-scope regression escalation (Wave-5 of the 2026-06-11 delivery P0)
# ─────────────────────────────────────────────────────────────────────────────


def _miss_row(bot: str, app: str, *, ts: datetime = NOW,
              outcome: str = dm.OUTCOME_MISSED, cause: str | None = None) -> dict:
    return {
        "ts": ts.isoformat(),
        "bot_id": bot,
        "app_id": app,
        "action_id": "act",
        "outcome": outcome,
        "suspected_cause": cause,
    }


@pytest.fixture()
def _no_oc_install(monkeypatch):
    """Keep the spec builder off the real filesystem."""
    monkeypatch.setattr(dm, "_installed_oc_info", lambda *a, **kw: None)


def test_pod_regression_fires_across_bots(tmp_path, _no_oc_install):
    rows = [
        _miss_row("bot_a", "app_briefing"),
        _miss_row("bot_a", "app_sweep"),
        _miss_row("bot_b", "app_briefing"),
    ]
    spec = dm.check_pod_regression(tmp_path, NOW, extra_rows=rows)
    assert spec is not None
    assert spec["type"] == dm.TYPE_POD_REGRESSION
    assert spec["scope"] == "pod"
    assert spec["severity"] == "alert"
    assert spec["producer"] == dm.PRODUCER
    assert "3 scheduled deliveries across 2 of your bots" in spec["body"]
    # No recent OpenClaw install info → the update must NOT be named.
    assert "OpenClaw" not in spec["body"]
    assert spec["details"]["missed_instances"] == [
        "bot_a/app_briefing", "bot_a/app_sweep", "bot_b/app_briefing",
    ]


def test_pod_regression_single_bot_never_fires(tmp_path, _no_oc_install):
    """Five misses on one bot is that bot's story, not a platform story."""
    rows = [_miss_row("bot_a", f"app_{i}") for i in range(5)]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_below_instance_threshold(tmp_path, _no_oc_install):
    """Two instances across two bots stays below the noise floor."""
    rows = [_miss_row("bot_a", "app_briefing"), _miss_row("bot_b", "app_sweep")]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_dedups_repeat_windows(tmp_path, _no_oc_install):
    """The same (bot, app) missing repeatedly is one instance, not many."""
    rows = [
        _miss_row("bot_a", "app_briefing", ts=NOW - timedelta(hours=h))
        for h in range(6)
    ] + [_miss_row("bot_b", "app_sweep")]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_excludes_benign_host_causes(tmp_path, _no_oc_install):
    """A 7am host-asleep (or DST) morning misses every bot's briefing
    without being a platform regression."""
    rows = [
        _miss_row("bot_a", "app_briefing", cause="host_asleep"),
        _miss_row("bot_b", "app_briefing", cause="host_asleep"),
        _miss_row("bot_a", "app_sweep", cause="dst"),
        _miss_row("bot_b", "app_sweep"),
    ]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_only_counts_misses(tmp_path, _no_oc_install):
    rows = [
        _miss_row("bot_a", "app_briefing", outcome=dm.OUTCOME_UNMEASURABLE),
        _miss_row("bot_a", "app_sweep", outcome=dm.OUTCOME_ON_TIME),
        _miss_row("bot_b", "app_briefing"),
        _miss_row("bot_b", "app_sweep"),
    ]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_old_rows_age_out(tmp_path, _no_oc_install):
    """Misses older than the rolling window don't keep the Signal alive."""
    old = NOW - timedelta(hours=dm.POD_REGRESSION_WINDOW_HOURS + 1)
    rows = [
        _miss_row("bot_a", "app_briefing", ts=old),
        _miss_row("bot_a", "app_sweep", ts=old),
        _miss_row("bot_b", "app_briefing", ts=old),
    ]
    assert dm.check_pod_regression(tmp_path, NOW, extra_rows=rows) is None


def test_pod_regression_reads_ledger_files(tmp_path, _no_oc_install):
    """Rows already on disk (today + yesterday) count — the aggregation
    must see misses recorded by earlier ticks, including yesterday's
    (rolling 24h, no midnight flap)."""
    yesterday = NOW - timedelta(hours=20)
    for row in (
        _miss_row("bot_a", "app_briefing", ts=yesterday),
        _miss_row("bot_a", "app_sweep", ts=yesterday),
    ):
        dm.append_ledger(tmp_path, row, yesterday)
    dm.append_ledger(tmp_path, _miss_row("bot_b", "app_briefing"), NOW)

    spec = dm.check_pod_regression(tmp_path, NOW, extra_rows=[])
    assert spec is not None
    assert spec["details"]["distinct_bots"] == ["bot_a", "bot_b"]


def test_pod_regression_skips_malformed_ledger_lines(tmp_path, _no_oc_install):
    ledger_dir = tmp_path / "delivery_monitor" / "ledger"
    ledger_dir.mkdir(parents=True)
    good = json.dumps(_miss_row("bot_b", "app_briefing"))
    (ledger_dir / f"{NOW.strftime('%Y-%m-%d')}.jsonl").write_text(
        f"not json\n{good}\n[1,2]\n", encoding="utf-8",
    )
    rows = [_miss_row("bot_a", "app_briefing"), _miss_row("bot_a", "app_sweep")]
    spec = dm.check_pod_regression(tmp_path, NOW, extra_rows=rows)
    assert spec is not None  # the good line still counted


def test_pod_regression_names_recent_oc_install(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dm, "_installed_oc_info",
        lambda *a, **kw: {
            "version": "2026.6.1",
            "installed_at": NOW - timedelta(days=2),
        },
    )
    rows = [
        _miss_row("bot_a", "app_briefing"),
        _miss_row("bot_a", "app_sweep"),
        _miss_row("bot_b", "app_briefing"),
    ]
    spec = dm.check_pod_regression(tmp_path, NOW, extra_rows=rows)
    assert spec is not None
    assert "OpenClaw update" in spec["body"]
    assert "2026.6.1" in spec["body"]
    assert spec["details"]["openclaw"]["recent"] is True


def test_pod_regression_stale_oc_install_not_blamed(tmp_path, monkeypatch):
    """An install older than the recency window must not be named — naming
    a months-old update would be confabulated causality."""
    monkeypatch.setattr(
        dm, "_installed_oc_info",
        lambda *a, **kw: {
            "version": "2026.4.13",
            "installed_at": NOW - timedelta(days=60),
        },
    )
    rows = [
        _miss_row("bot_a", "app_briefing"),
        _miss_row("bot_a", "app_sweep"),
        _miss_row("bot_b", "app_briefing"),
    ]
    spec = dm.check_pod_regression(tmp_path, NOW, extra_rows=rows)
    assert spec is not None
    assert "OpenClaw" not in spec["body"]
    assert spec["details"]["openclaw"]["recent"] is False


def test_installed_oc_info_reads_fixture(tmp_path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"version": "2026.6.5"}), encoding="utf-8")
    info = dm._installed_oc_info(candidates=(tmp_path / "missing", pkg))
    assert info is not None
    assert info["version"] == "2026.6.5"
    assert info["installed_at"].tzinfo is not None


def test_installed_oc_info_none_when_absent(tmp_path):
    assert dm._installed_oc_info(candidates=(tmp_path / "missing",)) is None


def test_run_fires_and_sweeps_pod_signal(tmp_path, monkeypatch):
    """run()-level: a seeded ledger crossing the threshold fires ONE
    pod-scope Signal; once the misses age past the rolling window the
    sweep archives it."""
    monkeypatch.setattr(dm, "_installed_oc_info", lambda *a, **kw: None)
    shared = tmp_path / "shared"
    shared.mkdir()
    for row in (
        _miss_row("bot_a", "app_briefing"),
        _miss_row("bot_a", "app_sweep"),
        _miss_row("bot_b", "app_briefing"),
    ):
        dm.append_ledger(shared, row, NOW)

    kept, _, _ = dm.run(shared, {"bots": {}}, probes=_probes(), now=NOW)
    pod_sigs = [
        s for s in signals_store.iter_active(shared, producer=dm.PRODUCER)
        if s.type == dm.TYPE_POD_REGRESSION
    ]
    assert len(pod_sigs) == 1
    assert pod_sigs[0].scope == "pod"
    assert pod_sigs[0].signature in kept

    # Two days later the misses have aged out → condition cleared → swept.
    later = NOW + timedelta(days=2)
    kept2, _, n_resolved = dm.run(shared, {"bots": {}}, probes=_probes(), now=later)
    assert pod_sigs[0].signature not in kept2
    assert n_resolved >= 1
    assert [
        s for s in signals_store.iter_active(shared, producer=dm.PRODUCER)
        if s.type == dm.TYPE_POD_REGRESSION
    ] == []


def test_ledger_row_carries_suspected_cause(tmp_path):
    """The pod aggregation's benign-cause exclusion depends on the ledger
    row carrying suspected_cause."""
    action = _action(tmp_path)
    cls = dm.Classification(
        dm.OUTCOME_MISSED, diagnosis=dm.DIAGNOSIS_DID_NOT_RUN,
        suspected_cause="host_asleep",
    )
    row = dm._ledger_row(action, None, cls)
    assert row["suspected_cause"] == "host_asleep"
