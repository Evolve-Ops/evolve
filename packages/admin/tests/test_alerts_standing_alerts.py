"""tests/test_alerts_standing_alerts.py — the standing-alerts inventory digest.

``signal_notifier`` announces transitions; nothing announced *inventory*, and
its cold-start guard permanently silenced whatever backlog existed at first
run (measured 2026-07-29: 95/99 firing Signals on the mini, 34/36 on the
Linux VPS had never been announced). ``alerts.standing_alerts`` is the
inventory channel. Pins:

  - content assembly: severity-then-age ordering, severity counts, top_n
    truncation with an accurate "+N more" tail, title clipping
  - the never-announced count is derived from the notifier state's
    ``cold_start_synced`` marker + ``Signal.deliveries`` — NOT from
    "an entry exists"
  - idempotence: a second run inside the window contributes nothing; ad-hoc
    (Manual / Dry-run) runs bypass the window and never advance it
  - empty backlog sends NOTHING and does not burn the window
  - remediation lines come from the Signal's own fix_steps / remediation /
    deeplink, and are honest ("no fix on file") when it has none
  - **delivery proof**: the assembled section reaches the wire through the
    REAL dispatcher on ``summaries.daily_pod_report`` (transport faked) and
    lands a record in ``alerts/dispatcher/<day>.jsonl`` — eligibility
    (not deny-listed) is necessary but not sufficient, per the
    ``repo_puller_sudoers`` incident where a DAILY_DIGEST-mapped alert fired
    12× in 24h with ``deliveries: []``
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_process_global_seams():
    """No test here sets a process-global seam (``set_perms`` / ``set_profile``).

    The fixture exists as the enforced teardown point: a leaked global from
    this module red-ed CI earlier in this arc, so if a future test in this
    file installs one, it resets here instead of bleeding into whatever runs
    next in the same worker.
    """
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


def _seed(shared, *, signature, severity="warn", age_hours=48.0,
          producer="pod_health", type_="gateway_down", bot_id="team_bot_a",
          title="Gateway probe failed (deep)", details=None,
          remediation=None):
    """Write a firing Signal through the real store, then backdate it."""
    import signals.store as signals_store
    sig = signals_store.observe(
        shared,
        signature=signature,
        producer=producer,
        type=type_,
        flavor="maintenance",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=title,
        body="HTTP probe failed on the gateway port",
        details=details or {},
        remediation=remediation,
    )
    created = (_NOW - timedelta(hours=age_hours)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    path = shared / "signals" / "firing" / f"{sig.id}.json"
    data = json.loads(path.read_text())
    data["created_at"] = created
    path.write_text(json.dumps(data, indent=2))
    return sig


def _write_notifier_state(shared, signatures):
    p = shared / "signals" / "notifier-state.json"
    p.write_text(json.dumps({"version": 1, "signatures": signatures}, indent=2))


def _sa():
    from evolve_admin.alerts import standing_alerts
    return standing_alerts


# ── Empty backlog: send NOTHING ─────────────────────────────────────────────


def test_empty_backlog_contributes_nothing(shared):
    sa = _sa()
    summary = sa.collect(shared, now=_NOW)
    assert summary.is_empty
    assert summary.total == 0
    assert sa.render(summary) == ""

    text, worst = sa.build_section(shared, label="Daily", now=_NOW)
    assert text == ""
    assert worst is None
    # And the window must NOT be burned — the next non-empty backlog is
    # reported immediately rather than waiting out an unused window.
    assert not sa.state_path(shared).exists()


def test_disabled_contributes_nothing(shared, monkeypatch):
    sa = _sa()
    _seed(shared, signature="pod_health:gateway_down:team_bot_a")
    monkeypatch.setattr(
        sa, "_read_enabled", lambda _shared_dir: False,
    )
    assert sa.build_section(shared, label="Daily", now=_NOW) == ("", None)


# ── Content assembly ────────────────────────────────────────────────────────


def test_ordering_counts_and_truncation(shared):
    """Worst severity first, then oldest; counts cover the WHOLE set and the
    "+N more" tail accounts for everything not listed."""
    sa = _sa()
    _seed(shared, signature="p:info_new", severity="info", age_hours=1,
          type_="info_new", title="Info, new")
    _seed(shared, signature="p:warn_old", severity="warn", age_hours=900,
          type_="warn_old", title="Warn, ancient")
    _seed(shared, signature="p:warn_young", severity="warn", age_hours=30,
          type_="warn_young", title="Warn, recent")
    _seed(shared, signature="p:alert_mid", severity="alert", age_hours=100,
          type_="alert_mid", title="Alert, mid")
    _seed(shared, signature="p:alert_old", severity="alert", age_hours=800,
          type_="alert_old", title="Alert, ancient")

    summary = sa.collect(shared, now=_NOW, top_n=2)
    assert summary.total == 5
    assert summary.by_severity == {"alert": 2, "warn": 2, "info": 1}
    assert [e.signature for e in summary.top] == ["p:alert_old", "p:alert_mid"]
    assert summary.remaining == 3
    # oldest = 900h ≈ 37d
    assert summary.oldest_age_seconds == pytest.approx(900 * 3600)

    text = sa.render(summary)
    lines = text.splitlines()
    assert lines[0].startswith("🔔 Standing alerts — 5 still open")
    assert "2 alert · 2 warn · 1 info" in lines[1]
    assert "oldest 37d" in lines[1]
    assert "Alert, ancient" in text and "Alert, mid" in text
    # Truncated entries are counted, not listed.
    assert "Warn, ancient" not in text
    assert "+3 more standing alerts — Alerts → Active" in text
    # Phone-readable: header + facts + 2 entries + tail.
    assert len(lines) == 5


def test_long_title_is_clipped(shared):
    sa = _sa()
    _seed(shared, signature="p:long", severity="alert",
          title="A" * 200)
    summary = sa.collect(shared, now=_NOW, top_n=1)
    assert len(summary.top[0].title) <= 65
    assert summary.top[0].title.endswith("…")


def test_pod_report_signals_are_counted_but_not_listed(shared):
    """pod_report mirrors its own report lines into the Signal store, so a
    pod_report Signal in the shortlist would print the same finding twice in
    one message — once as a bucket line, once here. Counted, not listed."""
    sa = _sa()
    _seed(shared, signature="pod_report:audit_critical:pod", severity="alert",
          producer="pod_report", type_="audit_critical", bot_id=None,
          title="Audit critical")
    _seed(shared, signature="p:other", severity="warn", type_="other",
          bot_id=None, title="Something else")

    summary = sa.collect(shared, now=_NOW, top_n=3)
    assert summary.total == 2, "the inventory must agree with the Alerts page"
    assert summary.by_severity == {"alert": 1, "warn": 1}
    assert [e.signature for e in summary.top] == ["p:other"]
    assert summary.remaining == 1
    # Still eligible to raise pod_report's overall status.
    assert summary.worst_never_announced_severity == "alert"
    text = sa.render(summary)
    assert "Audit critical" not in text
    assert "Something else" in text


def test_only_pod_report_signals_standing_renders_a_counts_only_section(shared):
    sa = _sa()
    _seed(shared, signature="pod_report:audit_critical:pod", severity="alert",
          producer="pod_report", type_="audit_critical", bot_id=None)
    text = sa.render(sa.collect(shared, now=_NOW, top_n=3))
    assert "🔔 Standing alerts — 1 still open" in text
    assert "more standing alert" not in text, "misleading +N with nothing listed"
    assert "Full list: Alerts → Active (1 alert)" in text


def test_no_more_tail_when_everything_is_listed(shared):
    sa = _sa()
    _seed(shared, signature="p:only", severity="alert")
    text = sa.render(sa.collect(shared, now=_NOW, top_n=5))
    assert "more standing alert" not in text
    assert "Full list: Alerts → Active (1 alert)" in text


# ── never-announced accounting ──────────────────────────────────────────────


def test_never_announced_counts_cold_start_silenced(shared):
    """The root cause, pinned. A cold-start-synced entry LOOKS alerted
    (``alerted_for_signal_id`` is set) but no message was ever sent — it must
    count as never announced, or the digest reproduces the blind spot."""
    sa = _sa()
    silenced = _seed(shared, signature="p:silenced", severity="alert")
    announced = _seed(shared, signature="p:announced", severity="warn",
                      type_="other", title="Announced one")
    _write_notifier_state(shared, {
        "p:silenced": {
            "alerted_for_signal_id": silenced.id,
            "last_fire_pushed_at": "2026-06-23T00:00:00Z",
            "cold_start_synced": True,
            "never_announced": True,
        },
        "p:announced": {
            "alerted_for_signal_id": announced.id,
            "alerted_severity": "warn",
            "last_fire_pushed_at": "2026-07-28T00:00:00Z",
        },
    })

    summary = sa.collect(shared, now=_NOW, top_n=5)
    assert summary.total == 2
    assert summary.never_announced == 1
    assert summary.cold_start_silenced == 1
    assert summary.worst_never_announced_severity == "alert"
    by_sig = {e.signature: e for e in summary.top}
    assert by_sig["p:silenced"].announced is False
    assert by_sig["p:announced"].announced is True

    text = sa.render(summary)
    assert "1 never announced here" in text
    assert "already open when alerting started" in text


def test_signal_with_no_notifier_entry_is_never_announced(shared):
    """Inside grace, flap-suppressed, or permanent-failure: firing with no
    fire-push recorded. Nothing was sent, so it is not announced."""
    sa = _sa()
    sig = _seed(shared, signature="p:grace", severity="warn")
    _write_notifier_state(shared, {
        "p:perm": {"permanent_failure_signal_id": sig.id},
    })
    summary = sa.collect(shared, now=_NOW)
    assert summary.never_announced == 1
    assert summary.cold_start_silenced == 0  # not a cold-start silence


def test_permanent_failure_entry_is_not_an_announcement(shared):
    sa = _sa()
    sig = _seed(shared, signature="p:perm", severity="alert")
    _write_notifier_state(shared, {
        "p:perm": {
            "permanent_failure_signal_id": sig.id,
            "last_permanent_failure_at": "2026-07-28T00:00:00Z",
        },
    })
    assert sa.collect(shared, now=_NOW).never_announced == 1


def test_recorded_delivery_counts_as_announced(shared):
    """``Signal.deliveries`` is the other positive proof (audit.py records
    real dispatches there)."""
    import signals.store as signals_store
    sa = _sa()
    sig = _seed(shared, signature="p:delivered", severity="alert")
    signals_store.record_delivery(sig, shared, channel="telegram")
    summary = sa.collect(shared, now=_NOW)
    assert summary.total == 1
    assert summary.never_announced == 0
    assert summary.worst_never_announced_severity is None
    assert "never announced" not in sa.render(summary)


# ── Remediation lines: existing only, never invented ────────────────────────


def test_fix_line_prefers_producer_fix_steps(shared):
    sa = _sa()
    _seed(shared, signature="p:fix", severity="alert",
          details={"fix_steps": "1. Reinstall the infra jobs\n2. Re-check"})
    entry = sa.collect(shared, now=_NOW, top_n=1).top[0]
    assert entry.fix == "Reinstall the infra jobs"


def test_fix_line_falls_back_to_remediation_then_deeplink_then_honesty(shared):
    from schema.signal import Remediation
    sa = _sa()
    _seed(shared, signature="p:rem", severity="alert",
          remediation=Remediation(kind="install_infra_jobs",
                                  label="Install infra jobs"))
    _seed(shared, signature="p:link", severity="warn", type_="t2",
          details={"deeplink": "alerts?signal=abc"})
    _seed(shared, signature="p:none", severity="info", type_="t3")
    by_sig = {e.signature: e for e in sa.collect(shared, now=_NOW, top_n=5).top}
    assert by_sig["p:rem"].fix == (
        "one-click fix on the Alerts page: Install infra jobs"
    )
    assert by_sig["p:link"].fix == "details at alerts?signal=abc"
    # Honest about having nothing — never a fabricated instruction.
    assert by_sig["p:none"].fix == "no fix on file"


def test_bot_prefix_is_not_doubled_in_the_line(shared):
    """Producers that bake the bot name into the title must not render as
    "team_bot_a: team_bot_a gateway down"."""
    sa = _sa()
    _seed(shared, signature="p:dup", severity="alert",
          title="team_bot_a: gateway down")
    text = sa.render(sa.collect(shared, now=_NOW, top_n=1))
    assert "team_bot_a: gateway down" in text
    assert "team_bot_a: team_bot_a" not in text


# ── Idempotence + cadence ───────────────────────────────────────────────────


def test_second_run_inside_window_contributes_nothing(shared):
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    first, worst = sa.build_section(shared, label="Daily", now=_NOW)
    assert first and worst == "alert"

    for offset in (timedelta(minutes=1), timedelta(hours=6),
                   timedelta(hours=23, minutes=59)):
        again = sa.build_section(shared, label="Daily", now=_NOW + offset)
        assert again == ("", None), f"re-emitted after {offset}"

    after = sa.build_section(
        shared, label="Daily", now=_NOW + timedelta(hours=24, minutes=1),
    )
    assert after[0], "window elapsed — must emit again"


def test_ad_hoc_run_bypasses_window_and_does_not_advance_it(shared):
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    manual, _ = sa.build_section(shared, label="Manual", now=_NOW)
    assert manual
    # Nothing recorded — a test-send must not eat the scheduled section.
    assert not sa.state_path(shared).exists()
    scheduled, _ = sa.build_section(
        shared, label="Daily", now=_NOW + timedelta(minutes=5),
    )
    assert scheduled

    # And an ad-hoc run inside a live window still renders.
    dry, _ = sa.build_section(
        shared, label="Dry-run", now=_NOW + timedelta(minutes=6),
    )
    assert dry


def test_admin_ui_status_poll_does_not_burn_the_window(shared):
    """``/api/reports-alerts/status`` calls run_report(label="Live") on every
    admin-UI poll. If an unrecognized label advanced the watermark, opening the
    Reports page in the morning would silently consume that day's standing
    section. Only a scheduled label advances it — allowlist, not deny-list."""
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    for label in ("Live", "Manual", "Dry-run", "SomeFutureLabel"):
        text, _ = sa.build_section(shared, label=label, now=_NOW)
        assert text, f"{label} should still render the current set"
        assert not sa.state_path(shared).exists(), f"{label} burned the window"

    scheduled, _ = sa.build_section(shared, label="Daily", now=_NOW)
    assert scheduled
    assert sa.state_path(shared).exists()


def test_zero_interval_emits_every_run(shared, monkeypatch):
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    monkeypatch.setattr(sa, "_read_min_interval_hours", lambda _sd: 0)
    assert sa.build_section(shared, label="Daily", now=_NOW)[0]
    assert sa.build_section(
        shared, label="Daily", now=_NOW + timedelta(minutes=1),
    )[0]


@pytest.mark.parametrize("watermark", [
    "not-a-timestamp",           # unparseable
    "2026-08-30T00:00:00Z",      # in the future (clock jump / restored state)
])
def test_untrustworthy_watermark_fails_open(shared, watermark):
    """Fail-closed here would silently withhold the section for a whole
    window — the exact failure mode this module exists to remove."""
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    sa.state_path(shared).write_text(json.dumps({
        "version": 1, "last_emitted_at": watermark,
    }))
    text, _ = sa.build_section(shared, label="Daily", now=_NOW)
    assert text, f"withheld the section on watermark {watermark!r}"


def test_naive_watermark_is_read_as_utc(shared):
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    sa.state_path(shared).write_text(json.dumps({
        "version": 1, "last_emitted_at": "2026-07-29T11:00:00",
    }))
    # 1h before _NOW, inside the 24h window — must be treated as UTC, not
    # raise on aware/naive comparison.
    assert sa.build_section(shared, label="Daily", now=_NOW) == ("", None)


def test_delta_against_the_previously_reported_set(shared):
    sa = _sa()
    _seed(shared, signature="p:keeps", severity="alert")
    _seed(shared, signature="p:clears", severity="warn", type_="t2")
    first, _ = sa.build_section(shared, label="Daily", now=_NOW)
    # First report has no prior set to compare against.
    assert "unchanged" not in first and "+0" not in first

    # Same set a day later → the message says so instead of repeating
    # verbatim as if it were fresh news.
    day2 = _NOW + timedelta(hours=25)
    second, _ = sa.build_section(shared, label="Daily", now=day2)
    assert "unchanged since the last report" in second

    # One clears, one arrives.
    (shared / "signals" / "firing").mkdir(parents=True, exist_ok=True)
    for p in (shared / "signals" / "firing").glob("*.json"):
        if json.loads(p.read_text())["signature"] == "p:clears":
            p.unlink()
    _seed(shared, signature="p:arrives", severity="warn", type_="t3")
    day3 = day2 + timedelta(hours=25)
    third, _ = sa.build_section(shared, label="Daily", now=day3)
    assert "+1 new" in third
    assert "-1 cleared" in third


def test_state_file_tracks_what_was_reported(shared):
    sa = _sa()
    _seed(shared, signature="p:one", severity="alert")
    sa.build_section(shared, label="Daily", now=_NOW)
    state = json.loads(sa.state_path(shared).read_text())
    assert state["last_total"] == 1
    assert state["last_never_announced"] == 1
    assert state["last_signatures"] == ["p:one"]
    assert state["last_emitted_at"].startswith("2026-07-29T12:00:00")


def test_oversized_set_drops_the_delta_instead_of_faking_it(shared, monkeypatch):
    """A truncated previous set would report the truncated tail as "cleared"
    and the same signatures as "new" every day. Drop the delta instead."""
    sa = _sa()
    monkeypatch.setattr(sa, "_MAX_TRACKED_SIGNATURES", 2)
    for i in range(3):
        _seed(shared, signature=f"p:{i}", severity="warn", type_=f"t{i}")
    first, _ = sa.build_section(shared, label="Daily", now=_NOW)
    assert first
    state = json.loads(sa.state_path(shared).read_text())
    assert "last_signatures" not in state
    assert state["last_signatures_omitted"] == 3

    second, _ = sa.build_section(
        shared, label="Daily", now=_NOW + timedelta(hours=25),
    )
    facts = second.splitlines()[1]
    assert "unchanged since the last report" not in facts
    assert " new" not in facts and " cleared" not in facts


# ── Delivery proof (not just eligibility) ───────────────────────────────────


def test_section_delivers_through_the_real_dispatcher(shared, monkeypatch):
    """The whole point of the PR: the assembled section must actually reach
    the operator.

    Runs the REAL ``dispatcher.send`` on the REAL catalog event pod_report
    uses (``summaries.daily_pod_report``) with only the transport faked, and
    asserts (a) DispatchResult.SENT, (b) the standing text is on the wire,
    (c) a delivery record landed in ``alerts/dispatcher/<day>.jsonl``.

    Non-membership in ``_DIRECT_DISPATCH_PRODUCERS`` proves nothing here:
    ``repo_puller_sudoers`` was not deny-listed and still sat undelivered
    for 24h+ because its catalog event was DAILY_DIGEST-mapped.
    ``summaries.daily_pod_report`` is ``Frequency.DAILY`` — a natural
    cadence the producer owns, which the dispatcher sends immediately rather
    than enqueueing.
    """
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.catalog import Frequency, by_key

    event = by_key("summaries.daily_pod_report")
    assert event is not None
    assert event.default_enabled is True
    assert event.default_frequency is Frequency.DAILY, (
        "a DAILY_DIGEST-mapped event would batch the inventory digest — the "
        "repo_puller_sudoers failure mode"
    )

    wire: list[tuple[str, str, str]] = []

    def _fake_transport(channel, chat_id, message, gateway_port=None):
        wire.append((channel, chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_openclaw", _fake_transport)

    sa = _sa()
    _seed(shared, signature="p:evo_path_down", severity="alert",
          producer="evo_path_probe", type_="evo_path_down", bot_id=None,
          title="The evo keyword path is down",
          details={"fix_steps": "1. Reinstall the infra jobs"})
    section, worst = sa.build_section(shared, label="Daily", now=_NOW)
    assert section and worst == "alert"

    outcome = dispatcher.send(
        shared_dir=shared,
        network={"alerts": {"channel": "telegram", "chatId": "12345"}},
        source="pod_report",
        severity=dispatcher.Severity.ERROR,
        dedup_key="pod_report/Daily",
        catalog_event="summaries.daily_pod_report",
        payload={"label": "Daily", "summary": f"Pod: quiet day\n\n{section}"},
    )

    assert outcome.result is dispatcher.DispatchResult.SENT, (
        f"standing-alerts section did not deliver: {outcome.result}"
    )
    assert len(wire) == 1, "exactly one operator message"
    _channel, _chat, message = wire[0]
    assert "Standing alerts — 1 still open" in message
    assert "The evo keyword path is down" in message
    assert "Reinstall the infra jobs" in message
    assert "subscription: summaries.daily_pod_report" in message

    # The dispatcher's delivery log — the recorded proof of a real send.
    records = list(dispatcher.iter_log_records(shared, "dispatcher.jsonl"))
    delivered = [
        r for r in records
        if r.get("catalog_event") == "summaries.daily_pod_report"
        and r.get("result") == "sent"
    ]
    assert delivered, (
        "no delivery record in alerts/dispatcher/<day>.jsonl — the "
        "repo_puller_sudoers shape (fired, never delivered)"
    )


def test_html_unsafe_signal_text_is_escaped_on_the_wire(shared, monkeypatch):
    """The section is plain text; catalog.render_event escapes payload
    values. A Signal title containing markup must not break parse_mode=HTML
    (and must not arrive double-escaped)."""
    from evolve_admin.alerts import dispatcher

    wire: list[str] = []
    monkeypatch.setattr(
        dispatcher, "_dispatch_via_openclaw",
        lambda channel, chat_id, message, gateway_port=None: (
            wire.append(message) or (True, None)
        ),
    )
    sa = _sa()
    _seed(shared, signature="p:html", severity="alert",
          bot_id=None, title="<b>gateway</b> & friends down")
    section, _ = sa.build_section(shared, label="Daily", now=_NOW)
    assert "<b>gateway</b> & friends down" in section, "section stays raw"

    dispatcher.send(
        shared_dir=shared,
        network={"alerts": {"channel": "telegram", "chatId": "12345"}},
        source="pod_report",
        severity=dispatcher.Severity.ERROR,
        dedup_key="pod_report/Daily",
        catalog_event="summaries.daily_pod_report",
        payload={"label": "Daily", "summary": section},
    )
    assert wire
    assert "&lt;b&gt;gateway&lt;/b&gt; &amp; friends down" in wire[0]
    assert "&amp;lt;" not in wire[0], "double-escaped"


# ── Config wiring ───────────────────────────────────────────────────────────


def test_schema_registers_the_operator_knobs():
    from evolve_admin.config_sandbox.schema import SCHEMA
    by_path = {e.path: e for e in SCHEMA}
    assert by_path["alerts.standing_alerts.enabled"].stock_default is True
    assert by_path["alerts.standing_alerts.min_interval_hours"].stock_default == 24
    assert by_path["alerts.standing_alerts.top_n"].stock_default == 3


def test_standing_alerts_is_not_a_second_dispatch_source():
    """It renders INTO the pod report; a source of its own would mean a
    second daily message."""
    from evolve_admin.alerts import dispatcher
    assert "standing_alerts" not in dispatcher._DEFAULT_SOURCE_ENABLED
