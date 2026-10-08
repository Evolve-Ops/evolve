"""pod_report carries the standing-alerts inventory section.

The firing-Signal backlog had no operator channel at all: signal_notifier
announces transitions, and its cold-start guard permanently silences whatever
was already firing at first run (95/99 firing Signals on the mini, 34/36 on
the Linux VPS on 2026-07-29 had never been announced). Rather than add a
daemon, the inventory rides the daily pod report — the one message the
operator already reads, already dispatched on a natural daily cadence via
``summaries.daily_pod_report``.

Pins:

  - the section is appended to the body, after today's news
  - an empty backlog changes the report not at all
  - a never-announced ``alert`` raises ``overall`` to red so a
    ``notify_on: red_only`` gate can't hide it; a never-announced ``warn``
    raises it to yellow; ``info`` and already-announced sets don't move it
  - ``run_report`` survives a broken standing-alerts collector
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pod_report  # noqa: E402

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_leaked_process_globals():
    """No test here installs a process-global seam (``set_perms`` /
    ``set_profile``); this is the enforced teardown if one ever does. A leak
    of exactly this kind red-ed CI earlier in this arc."""
    yield
    for mod_name, resetter in (
        ("evolve_admin.linux_perms", "set_perms"),
        ("evolve_admin.platform_profile", "set_profile"),
    ):
        mod = sys.modules.get(mod_name)
        fn = getattr(mod, resetter, None) if mod is not None else None
        if fn is not None:
            try:
                fn(None)
            except Exception:  # noqa: BLE001 — teardown must never fail a test
                pass


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (s / "signals" / sub).mkdir(parents=True)
    return s


def _seed(shared, *, signature, severity, announced=False, age_hours=48.0):
    import signals.store as signals_store
    sig = signals_store.observe(
        shared,
        signature=signature,
        producer="evo_path_probe",
        type=signature.replace(":", "_"),
        flavor="maintenance",
        severity=severity,
        scope="pod",
        title="The evo keyword path is down",
        body="The evo keyword path is down",
        details={"fix_steps": "1. Reinstall the infra jobs"},
    )
    path = shared / "signals" / "firing" / f"{sig.id}.json"
    data = json.loads(path.read_text())
    data["created_at"] = (
        (_NOW - timedelta(hours=age_hours))
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    path.write_text(json.dumps(data, indent=2))
    if announced:
        p = shared / "signals" / "notifier-state.json"
        state = json.loads(p.read_text()) if p.exists() else {
            "version": 1, "signatures": {},
        }
        state["signatures"][signature] = {
            "alerted_for_signal_id": sig.id,
            "alerted_severity": severity,
            "last_fire_pushed_at": "2026-07-28T00:00:00Z",
        }
        p.write_text(json.dumps(state, indent=2))
    return sig


# ── render_report wiring ────────────────────────────────────────────────────


def test_standing_section_is_appended_after_todays_news():
    news = pod_report.ReportLine(
        bucket="broken", severity="red", text="gateway down on team_bot_a",
        signal_type="gateway_down",
    )
    text, overall = pod_report.render_report(
        "Daily", [news], [], [], "",
        pod_usage_line="Pod: 12 turns yesterday",
        standing_section="🔔 Standing alerts — 4 still open",
        standing_worst_unannounced="alert",
    )
    assert text.index("gateway down") < text.index("Standing alerts")
    assert text.startswith("Pod: 12 turns yesterday")
    assert overall == "red"


def test_empty_standing_section_changes_nothing():
    baseline = pod_report.render_report(
        "Daily", [], [], [], "", pod_usage_line="Pod: quiet",
    )
    with_empty = pod_report.render_report(
        "Daily", [], [], [], "", pod_usage_line="Pod: quiet",
        standing_section="", standing_worst_unannounced=None,
    )
    assert with_empty == baseline
    assert with_empty[1] == "green"


@pytest.mark.parametrize("worst,expected", [
    ("alert", "red"),      # never announced + alert → survives red_only
    ("warn", "yellow"),    # → survives yellow_or_red
    ("info", "green"),     # low-stakes; yellow_or_red opted out of this tier
    (None, "green"),       # everything standing was already announced
])
def test_never_announced_severity_drives_overall(worst, expected):
    _text, overall = pod_report.render_report(
        "Daily", [], [], [], "",
        standing_section="🔔 Standing alerts — 1 still open",
        standing_worst_unannounced=worst,
    )
    assert overall == expected


def test_standing_section_never_downgrades_a_red_report():
    news = pod_report.ReportLine(
        bucket="broken", severity="red", text="gateway down",
        signal_type="gateway_down",
    )
    _text, overall = pod_report.render_report(
        "Daily", [news], [], [], "",
        standing_section="🔔 Standing alerts — 1 still open",
        standing_worst_unannounced=None,
    )
    assert overall == "red"


# ── run_report integration ──────────────────────────────────────────────────


def test_run_report_includes_the_standing_section(shared):
    _seed(shared, signature="evo:path_down", severity="alert")
    _seed(shared, signature="evo:config_invalid", severity="warn")
    text, _overall, structured = pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
        now=datetime(2026, 7, 29, 12, 0, 0),
    )
    assert "🔔 Standing alerts — 2 still open" in text
    assert "2 never announced here" in text
    assert "Reinstall the infra jobs" in text
    assert structured["standing_section"]
    # ``overall`` is asserted against the standing set in isolation by the
    # render_report cases above — a bare tmp shared_dir also has no audit
    # snapshot, which fires pod_report's own audit_missing line.


def test_run_report_is_silent_on_an_empty_backlog(shared):
    text, _overall, structured = pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
        now=datetime(2026, 7, 29, 12, 0, 0),
    )
    assert "Standing alerts" not in text
    assert structured["standing_section"] == ""


def test_run_report_does_not_repeat_the_section_within_the_window(shared):
    """The pod-report LaunchDaemon ticks hourly and ``should_run`` gates on
    ``report_hour``; if the section were unconditional, a second qualifying
    run (a Manual send, a clock/DST edge, an operator changing report_hour)
    would repeat it. ``run_report``'s ``now`` is threaded into the gate so
    this is testable without waiting on the wall clock."""
    _seed(shared, signature="evo:path_down", severity="alert")
    when = datetime(2026, 7, 29, 12, 0, 0)
    first, _o, _s = pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily", now=when,
    )
    assert "Standing alerts" in first
    for offset in (timedelta(hours=1), timedelta(hours=23)):
        again, _o2, _s2 = pod_report.run_report(
            shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
            now=when + offset,
        )
        assert "Standing alerts" not in again, f"repeated after {offset}"
    # Next day: reported again.
    tomorrow, _o3, _s3 = pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
        now=when + timedelta(hours=25),
    )
    assert "Standing alerts" in tomorrow


def test_run_report_survives_a_broken_standing_collector(shared, monkeypatch):
    """Best-effort: a standing-alerts failure must never cost the operator
    the rest of the report."""
    from evolve_admin.alerts import standing_alerts

    def _boom(*_a, **_kw):
        raise RuntimeError("signal store unreadable")

    monkeypatch.setattr(standing_alerts, "build_section", _boom)
    text, overall, structured = pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
        now=datetime(2026, 7, 29, 12, 0, 0),
    )
    assert structured["standing_section"] == ""
    assert "Standing alerts" not in text
    # The rest of the report still built — the buckets and overall status
    # are unaffected by the standing-alerts failure.
    assert overall in ("red", "yellow", "green")
    assert structured["buckets"]["broken"]


def test_standing_section_is_not_mirrored_back_into_the_signal_store(shared):
    """The section is a *report* of Signals, not a Signal. Emitting one would
    add a firing Signal about the firing backlog — and, being firing, it
    would itself never be announced."""
    import signals.store as signals_store

    _seed(shared, signature="evo:path_down", severity="alert")
    pod_report.run_report(
        shared, [], pod_report.DEFAULT_OVERRIDES, "Daily",
        now=datetime(2026, 7, 29, 12, 0, 0),
    )
    producers = {
        s.producer
        for s in signals_store.iter_signals(shared, subdirs=("firing",))
    }
    assert "standing_alerts" not in producers
