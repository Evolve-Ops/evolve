"""tests/test_alerts_loop_monitor.py — alerts_loop_monitor producer tests.

Exercises the two detectors against hand-rolled dispatcher-log fixtures
plus an end-to-end runner test that verifies Signals get written to a
tmp shared_dir and sweep-resolve archives them when the loop clears.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import alerts_loop_monitor as alm  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


def _rec(
    *,
    minutes_ago: int = 0,
    source: str = "heal",
    result: str = "sent",
    severity: str = "warn",
    channel: str = "telegram",
    message_excerpt: str = "Config drift on evolve A protected configuration file changed",
    error: str | None = None,
    dedup_key: str | None = None,
) -> dict:
    ts = (_NOW - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    out: dict = {
        "ts": ts,
        "source": source,
        "severity": severity,
        "result": result,
        "channel": channel,
        "chat_id": "fake-chat-id",
        "message_excerpt": message_excerpt,
    }
    if error is not None:
        out["error"] = error
    if dedup_key is not None:
        out["dedup_key"] = dedup_key
    return out


def _write_log(shared_dir: Path, records: list[dict]) -> Path:
    log_path = shared_dir / "alerts" / "dispatcher.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return log_path


# ── Log reader ──────────────────────────────────────────────────────────────


def test_read_recent_records_filters_by_window(tmp_path):
    _write_log(
        tmp_path,
        [
            _rec(minutes_ago=10),
            _rec(minutes_ago=2 * 24 * 60),  # 2 days ago — outside 24h window
            _rec(minutes_ago=60),
        ],
    )
    since = _NOW - timedelta(hours=24)
    out = alm.read_recent_records(
        tmp_path / "alerts" / alm.LOG_FILENAME, since=since, now=_NOW
    )
    assert len(out) == 2


def test_read_recent_records_handles_missing_file(tmp_path):
    assert (
        alm.read_recent_records(
            tmp_path / "alerts" / alm.LOG_FILENAME,
            since=_NOW - timedelta(hours=24),
            now=_NOW,
        )
        == []
    )


def test_read_recent_records_reads_date_partitioned_dir(tmp_path):
    """Post-rotation, ``read_recent_records`` reads
    ``alerts/dispatcher/<YYYY-MM-DD>.jsonl`` partition files in addition
    to the legacy flat path. Anchor: 2026-06-01 rotation rollout."""
    log_dir = tmp_path / "alerts" / "dispatcher"
    log_dir.mkdir(parents=True)
    today_iso = _NOW.date().isoformat()
    yesterday_iso = (_NOW - timedelta(days=1)).date().isoformat()
    (log_dir / f"{today_iso}.jsonl").write_text(
        json.dumps(_rec(minutes_ago=30)) + "\n", encoding="utf-8",
    )
    (log_dir / f"{yesterday_iso}.jsonl").write_text(
        json.dumps(_rec(minutes_ago=23 * 60)) + "\n", encoding="utf-8",
    )

    out = alm.read_recent_records(
        tmp_path / "alerts" / alm.LOG_FILENAME,
        since=_NOW - timedelta(hours=24),
        now=_NOW,
    )
    assert len(out) == 2


def test_read_recent_records_skips_pre_window_date_files(tmp_path):
    """Day files dated before the since-day cutoff aren't opened —
    saves IO on a multi-month deployment where most partitions are
    older than the 24h scan window."""
    log_dir = tmp_path / "alerts" / "dispatcher"
    log_dir.mkdir(parents=True)
    ancient_iso = (_NOW - timedelta(days=60)).date().isoformat()
    # File contents that WOULD match the window if read.
    (log_dir / f"{ancient_iso}.jsonl").write_text(
        json.dumps(_rec(minutes_ago=10)) + "\n", encoding="utf-8",
    )
    out = alm.read_recent_records(
        tmp_path / "alerts" / alm.LOG_FILENAME,
        since=_NOW - timedelta(hours=24),
        now=_NOW,
    )
    # Pre-window day file skipped by filename — record never surfaces
    # despite its ts technically falling inside the window.
    assert out == []


def test_read_recent_records_skips_malformed_lines(tmp_path):
    log_path = tmp_path / "alerts" / alm.LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-05-17T11:30:00Z","source":"heal","result":"sent","message_excerpt":"ok"}\n'
        "not json at all\n"
        "\n"  # blank
        '{"missing_ts":true}\n'
        '{"ts":"2026-05-17T11:45:00Z","source":"heal","result":"sent","message_excerpt":"ok"}\n',
        encoding="utf-8",
    )
    out = alm.read_recent_records(
        log_path, since=_NOW - timedelta(hours=24), now=_NOW
    )
    # Two well-formed records; one missing ts is silently dropped; the
    # bare strings + blank lines are skipped.
    assert len(out) == 2


# ── detect_dispatcher_failures ───────────────────────────────────────────────


def test_failures_silent_below_warn_threshold():
    recs = [
        _rec(result="failed", source="report", error="timeout") for _ in range(2)
    ]
    out = alm.detect_dispatcher_failures(recs)
    assert out == []


def test_failures_warn_at_threshold():
    recs = [
        _rec(result="failed", source="update_watcher", error="timeout")
        for _ in range(4)
    ]
    out = alm.detect_dispatcher_failures(recs)
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "dispatcher_failures"
    assert sig["severity"] == "warn"
    assert sig["details"]["source"] == "update_watcher"
    assert sig["details"]["failure_count"] == 4
    assert "update_watcher" in sig["title"]


def test_failures_escalates_to_alert_at_higher_threshold():
    recs = [
        _rec(result="failed", source="update_watcher", error="timeout")
        for _ in range(12)
    ]
    out = alm.detect_dispatcher_failures(recs)
    assert out[0]["severity"] == "alert"


def test_failures_groups_by_source():
    recs = (
        [_rec(result="failed", source="update_watcher", error="timeout") for _ in range(3)]
        + [_rec(result="failed", source="pod_report", error="boom") for _ in range(4)]
        + [_rec(result="sent", source="heal")]
    )
    out = alm.detect_dispatcher_failures(recs)
    sources = sorted(d["details"]["source"] for d in out)
    assert sources == ["pod_report", "update_watcher"]


def test_failures_uses_latest_error_for_body():
    recs = [
        _rec(result="failed", source="x", error="old-error", minutes_ago=120),
        _rec(result="failed", source="x", error="newest-error", minutes_ago=5),
        _rec(result="failed", source="x", error="middle-error", minutes_ago=60),
    ]
    out = alm.detect_dispatcher_failures(recs)
    assert out[0]["details"]["latest_error"] == "newest-error"


# ── Actionability: target naming + error-derived remediation (#3152) ─────────


def test_failures_chat_not_found_names_target_and_start_step():
    """The live #3152 shape: Telegram 'chat not found' means the operator
    never /start-ed the bot. The card must name the delivery target and
    spell out the /start remediation."""
    recs = [
        _rec(
            result="failed", source="signal_notifier", channel="telegram",
            error="telegram http 400: Bad Request: chat not found",
        )
        for _ in range(12)
    ]
    # vary chat_id so the target label is deterministic
    for r in recs:
        r["chat_id"] = "987654321"
    out = alm.detect_dispatcher_failures(recs)
    assert len(out) == 1
    sig = out[0]
    d = sig["details"]
    # alert severity (12 >= alert_threshold) → DOWN framing
    assert sig["severity"] == "alert"
    assert "telegram:987654321" in sig["title"]
    assert "DOWN" in sig["title"]
    # remediation derived + carried structured
    assert d["failure_reason"] == "chat_not_found"
    assert "telegram:987654321" == d["target"]
    rem = d["remediation"].lower()
    assert "/start" in rem
    assert "restart the gateway" in rem
    # remediation also surfaces in the rendered "How to fix" section
    assert "/start" in d["fix_steps"].lower()
    # body names target + the /start step + scale + since
    assert "telegram:987654321" in sig["body"]
    assert "/start" in sig["body"].lower()
    assert "12" in sig["body"]  # undelivered count
    assert "First failure in window" in sig["body"]


def test_failures_blocked_bot_remediation():
    recs = [
        _rec(
            result="failed", source="signal_notifier", channel="telegram",
            error="telegram http 403: Forbidden: bot was blocked by the user",
        )
        for _ in range(4)
    ]
    out = alm.detect_dispatcher_failures(recs)
    d = out[0]["details"]
    assert d["failure_reason"] == "bot_blocked"
    assert "unblock" in d["remediation"].lower()


def test_failures_bad_token_remediation():
    recs = [
        _rec(
            result="failed", source="signal_notifier", channel="telegram",
            error="telegram http 401: Unauthorized",
        )
        for _ in range(5)
    ]
    out = alm.detect_dispatcher_failures(recs)
    d = out[0]["details"]
    assert d["failure_reason"] == "bad_token"
    assert "token" in d["remediation"].lower()
    assert "openclaw.json" in d["remediation"]


def test_failures_unrecognized_error_gets_generic_remediation():
    """An error string we don't have a specific mapping for must still
    produce a sane, actionable remediation that points at the Delivery
    log and echoes the captured error."""
    recs = [
        _rec(
            result="failed", source="report", channel="telegram",
            error="connection reset by peer",
        )
        for _ in range(4)
    ]
    out = alm.detect_dispatcher_failures(recs)
    d = out[0]["details"]
    assert d["failure_reason"] == "unknown"
    assert "Delivery log" in d["remediation"]
    # echoes the original error so the operator isn't flying blind
    assert "connection reset by peer" in d["remediation"]


def test_failures_remediation_survives_missing_error():
    """A FAILED record with no error string at all must not crash the
    parser and should still carry a usable remediation."""
    recs = [
        _rec(result="failed", source="report", channel="telegram", error=None)
        for _ in range(4)
    ]
    # _rec omits the error key when None; make sure the detector copes.
    out = alm.detect_dispatcher_failures(recs)
    assert len(out) == 1
    d = out[0]["details"]
    assert d["failure_reason"] == "unknown"
    assert d["remediation"]  # non-empty
    assert "Delivery log" in d["remediation"]


def test_failures_signature_unchanged_no_fanout():
    """Dedup is by signature = (producer, 'dispatcher_failures', source).
    Per-target framing must NOT split one source into multiple signals or
    change the signature shape, or the card would fan out / lose dedup."""
    recs = (
        [
            _rec(
                result="failed", source="signal_notifier", channel="telegram",
                error="telegram http 400: Bad Request: chat not found",
            )
            for _ in range(6)
        ]
        # same source, a different target — still ONE signal, ONE signature
        + [
            dict(
                _rec(
                    result="failed", source="signal_notifier", channel="slack",
                    error="invalid_webhook",
                ),
                chat_id="C0123",
            )
            for _ in range(3)
        ]
    )
    out = alm.detect_dispatcher_failures(recs)
    assert len(out) == 1
    assert (
        out[0]["signature"]
        == alm.make_signature(alm.PRODUCER, "dispatcher_failures", "signal_notifier")
    )
    # multi-target clusters are flagged in the body
    assert "distinct targets" in out[0]["body"]


def test_failures_title_is_count_agnostic():
    """The title must not embed the raw failure count — it's refreshed on
    every observe() bump and the count increments per send, so embedding
    it would churn the card every monitor pass. Scale is conveyed by the
    severity word (failing / DOWN) instead."""
    small = alm.detect_dispatcher_failures(
        [_rec(result="failed", source="x", channel="telegram", error="boom")
         for _ in range(4)]
    )
    big = alm.detect_dispatcher_failures(
        [_rec(result="failed", source="x", channel="telegram", error="boom")
         for _ in range(40)]
    )
    # No digit from the count leaks into either title.
    assert "4" not in small[0]["title"]
    assert "40" not in big[0]["title"]
    assert "failing" in small[0]["title"]
    assert "DOWN" in big[0]["title"]


def test_target_label_helper():
    assert alm._target_label("telegram", "12345") == "telegram:12345"
    assert alm._target_label("telegram", None) == "telegram"
    assert alm._target_label("telegram", "") == "telegram"
    assert alm._target_label(None, None) == "unknown"


def test_classify_failure_never_raises_on_odd_input():
    # Empty, None, and unexpected types must all return a 3-tuple.
    for err in (None, "", "   ", "weird \x00 bytes"):
        reason, cause, rem = alm._classify_failure(err, "telegram")
        assert isinstance(reason, str) and reason
        assert isinstance(cause, str) and cause
        assert isinstance(rem, str) and rem


# ── detect_alert_repeat_loop ─────────────────────────────────────────────────


def test_repeat_loop_below_threshold_silent():
    recs = [_rec(source="heal") for _ in range(4)]
    assert alm.detect_alert_repeat_loop(recs) == []


def test_repeat_loop_fires_at_threshold():
    recs = [_rec(source="heal") for _ in range(6)]
    out = alm.detect_alert_repeat_loop(recs)
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "alert_repeat_loop"
    assert sig["severity"] == "warn"
    assert sig["details"]["source"] == "heal"
    assert sig["details"]["repeat_count"] == 6


def test_repeat_loop_groups_by_excerpt_hash():
    # Same source, two distinct message contents — should produce two
    # signals when each crosses the threshold.
    recs = (
        [_rec(source="heal", message_excerpt="Config drift on evolve foo") for _ in range(6)]
        + [_rec(source="heal", message_excerpt="Restart loop on team_bot_b bar") for _ in range(7)]
    )
    out = alm.detect_alert_repeat_loop(recs)
    assert len(out) == 2
    counts = sorted(d["details"]["repeat_count"] for d in out)
    assert counts == [6, 7]


def test_repeat_loop_ignores_periodic_sources_by_default():
    # 'report' is in the default ignore list — daily digests legitimately
    # repeat; we don't flag them as loop noise.
    recs = [_rec(source="report") for _ in range(20)]
    assert alm.detect_alert_repeat_loop(recs) == []


def test_repeat_loop_ignore_list_is_case_insensitive():
    recs = [_rec(source="POD_REPORT") for _ in range(10)]
    assert alm.detect_alert_repeat_loop(recs) == []


def test_repeat_loop_skips_failed_records():
    # Failures belong to detect_dispatcher_failures; the repeat-loop
    # detector should only count SENT (the operator IS being notified).
    recs = [_rec(source="heal", result="failed", error="x") for _ in range(8)]
    assert alm.detect_alert_repeat_loop(recs) == []


def test_repeat_loop_custom_ignore_list():
    recs = [_rec(source="heal") for _ in range(6)]
    out = alm.detect_alert_repeat_loop(recs, ignore_sources=["heal"])
    assert out == []


# ── End-to-end run() ─────────────────────────────────────────────────────────


def test_run_writes_signals_and_returns_kept(tmp_path):
    _write_log(
        tmp_path,
        # 6 sent heal messages (loop) + 4 failed update_watcher messages.
        [_rec(source="heal", minutes_ago=i * 10) for i in range(6)]
        + [
            _rec(source="update_watcher", result="failed", error="timeout", minutes_ago=i * 5)
            for i in range(4)
        ],
    )

    kept, n_fired, n_resolved = alm.run(tmp_path, config={}, now=_NOW)
    assert n_fired == 2
    assert n_resolved == 0
    assert len(kept) == 2

    sigs = list(signals_store.iter_active(tmp_path, producer="alerts_loop_monitor"))
    types = sorted(s.type for s in sigs)
    assert types == ["alert_repeat_loop", "dispatcher_failures"]


def test_run_sweep_resolve_archives_signals_when_log_clears(tmp_path):
    # Run 1: log shows the loop pattern.
    _write_log(tmp_path, [_rec(source="heal", minutes_ago=i * 10) for i in range(6)])
    kept1, _, _ = alm.run(tmp_path, config={}, now=_NOW)
    assert len(kept1) == 1
    assert (
        len(list(signals_store.iter_active(tmp_path, producer="alerts_loop_monitor")))
        == 1
    )

    # Run 2: log now empty (cleared). Sweep should resolve the prior firing.
    _write_log(tmp_path, [])
    kept2, n_fired2, n_resolved2 = alm.run(tmp_path, config={}, now=_NOW)
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert (
        list(signals_store.iter_active(tmp_path, producer="alerts_loop_monitor"))
        == []
    )


def test_run_uses_config_thresholds(tmp_path):
    # 4 same-content sent messages — below default repeat_threshold (5),
    # but a lower config threshold should fire.
    _write_log(
        tmp_path,
        [_rec(source="heal", minutes_ago=i * 10) for i in range(4)],
    )
    network = {"alerts_loop_monitor": {"repeat_threshold": 3}}
    kept, n_fired, _ = alm.run(tmp_path, config=network, now=_NOW)
    assert n_fired == 1
    assert len(kept) == 1


def test_run_dry_run_does_not_write_signals(tmp_path, capsys):
    _write_log(tmp_path, [_rec(source="heal", minutes_ago=i * 10) for i in range(6)])
    kept, n_fired, n_resolved = alm.run(
        tmp_path, config={}, dry_run=True, now=_NOW
    )
    assert n_fired == 1
    assert n_resolved == 0
    # Nothing landed on disk.
    assert (
        list(signals_store.iter_active(tmp_path, producer="alerts_loop_monitor"))
        == []
    )


def test_run_handles_missing_log_silently(tmp_path):
    kept, n_fired, n_resolved = alm.run(tmp_path, config={}, now=_NOW)
    assert kept == set()
    assert n_fired == 0
    assert n_resolved == 0


def test_severity_default_resolves_to_warn():
    """Severity policy in producer_severity should default this producer to warn."""
    from signals.producer_severity import default_severity

    assert default_severity("alerts_loop_monitor") == "warn"


def test_run_reads_failures_from_delivery_failures_jsonl(tmp_path):
    """After the PWA-polish file split, FAILED records land in
    ``delivery-failures.jsonl`` rather than ``dispatcher.jsonl``. The
    monitor must still detect them — otherwise the "N failed sends in
    24h" signal silently stops firing.

    This is the explicit silent-failure mode the PR brief warned about:
    moving a file location without updating every consumer of that
    file. Pin it here.
    """
    # Write success records to dispatcher.jsonl (where they actually live now).
    _write_log(tmp_path, [_rec(source="heal", result="sent", minutes_ago=i * 10) for i in range(6)])
    # Write FAILURE records to delivery-failures.jsonl (where they actually live now).
    failures_path = tmp_path / "alerts" / alm.FAILURES_LOG_FILENAME
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    with failures_path.open("w", encoding="utf-8") as fh:
        for i in range(4):
            r = _rec(
                source="update_watcher", result="failed",
                error="telegram http 400", minutes_ago=i * 5,
            )
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    kept, n_fired, n_resolved = alm.run(tmp_path, config={}, now=_NOW)
    sigs = list(signals_store.iter_active(tmp_path, producer="alerts_loop_monitor"))
    types = sorted(s.type for s in sigs)
    # Both detectors fire: alert_repeat_loop from dispatcher.jsonl SENT
    # records, dispatcher_failures from delivery-failures.jsonl entries.
    assert types == ["alert_repeat_loop", "dispatcher_failures"]
    failure_sig = next(s for s in sigs if s.type == "dispatcher_failures")
    assert failure_sig.details["source"] == "update_watcher"
    assert failure_sig.details["failure_count"] == 4
