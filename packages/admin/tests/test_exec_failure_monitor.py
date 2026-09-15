"""tests/test_exec_failure_monitor.py — absorbed exec-failure aggregation.

Design: internal/design-exec-failure-hygiene-2026-08-31.md (A2). The
plugin-side ExecFailureAbsorber ledgers every raw ``⚠️ 🛠️ Exec failed …``
trailer it sees on a channel-bound payload; this producer folds those
ledgers into dedup'd per-shape Signals. These tests pin:

  - repeated occurrences of the same failure SHAPE (differing commands /
    exit codes / ids) fold into ONE bot_exec_failures Signal whose
    details carry the in-window count
  - distinct shapes get distinct Signals; distinct bots never share one
  - shape derivation is value-free (inline code spans, numbers, hex ids
    collapsed)
  - a shape absent from the scan window sweep-resolves; a ledger READ
    error skips the sweep (unknown state is not "cleared")
  - rows older than the window are ignored; malformed rows (including a
    naive timestamp) never kill the sweep
  - observe-only vs armed rows both count, and details.absorb_armed
    reflects whether any occurrence was actually absorbed
  - the hourly audit-scheduler tick runs the sweep (Phase 0e) and records
    errors without failing the tick
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.exec_failure_monitor import (  # noqa: E402
    PRODUCER,
    TYPE_SHAPE,
    _shape_of,
    emit_exec_failure_signals,
)

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _make_shared(tmp_path: Path) -> Path:
    shared = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True)
    return shared


def _ledger_row(
    line: str,
    *,
    ts: datetime = NOW,
    action: str = "would_absorb",
    armed: bool = False,
    channel: str = "telegram",
) -> dict:
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "bot_id": "unused-here",
        "action": action,
        "armed": armed,
        "channel": channel,
        "to": "12345",
        "session_key": "agent:main:tg",
        "matched_lines": [line],
    }


def _write_ledger(shared: Path, bot: str, rows: list[dict], *, when: datetime = NOW) -> None:
    ledger_dir = shared / bot / "exec-failures"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    f = ledger_dir / f"exec-failures-{when.strftime('%Y-%m-%d')}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


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


# ── Shape derivation ─────────────────────────────────────────────────────────

def test_shape_is_value_free_but_keeps_the_command_word():
    a = _shape_of("⚠️ 🛠️ Exec failed: `ls /a` (exit 1)")
    b = _shape_of("⚠️ 🛠️ Exec failed: `ls /some/other/path` (exit 2)")
    assert a == b
    assert "`ls …`" in a and "/a" not in a
    # A different command is a DIFFERENT shape (A3 thresholds are per
    # failure class, not per bot).
    c = _shape_of("⚠️ 🛠️ Exec failed: `git push origin` (exit 128)")
    assert c != a
    d = _shape_of("⚠️ 🛠️ Bash failed: command not found")
    assert d != a


def test_shape_collapses_hex_ids_and_numbers_case_insensitively():
    a = _shape_of("Exec failed (0f3a2b1cdeadbeef, exit 1) :: boom 42")
    b = _shape_of("Exec failed (99CAFE00AA55BB77, exit 127) :: boom 7")
    assert a == b
    assert "0f3a2b1c" not in a and "42" not in a and "CAFE" not in a


# ── Aggregation ──────────────────────────────────────────────────────────────

def test_same_shape_folds_into_one_signal(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `ls /a` (exit 1)"),
        _ledger_row("⚠️ 🛠️ Exec failed: `ls /b` (exit 2)"),
        _ledger_row("⚠️ 🛠️ Exec failed: `ls /c` (exit 1)"),
    ])
    summary = emit_exec_failure_signals(shared, now=NOW)
    assert summary == {"bots": 1, "lines": 3, "shapes": 1, "read_errors": 0}
    sigs = _firing(shared)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == TYPE_SHAPE
    assert sig.scope == "bot"
    assert sig.bot_id == "team_bot_a"
    assert sig.details["count_in_window"] == 3
    assert sig.details["absorb_armed"] is False
    assert "observe-only" in sig.body
    # The raw matched line stays in the LEDGER; Signals carry only the
    # value-free shape (a failing argv can embed a secret).
    assert "/a" not in sig.body and "/c" not in sig.body
    assert "sample_line" not in sig.details
    assert "exec-failures" in sig.details["ledger"]


def test_distinct_shapes_and_bots_get_distinct_signals(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"),
        _ledger_row("⚠️ 🛠️ Bash failed: command not found"),
    ])
    _write_ledger(shared, "team_bot_b", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"),
    ])
    summary = emit_exec_failure_signals(shared, now=NOW)
    assert summary["shapes"] == 3
    sigs = _firing(shared)
    assert len(sigs) == 3
    assert {s.bot_id for s in sigs} == {"team_bot_a", "team_bot_b"}


def test_repeat_run_dedups_by_signature(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"),
    ])
    emit_exec_failure_signals(shared, now=NOW)
    emit_exec_failure_signals(shared, now=NOW)
    assert len(_firing(shared)) == 1


def test_armed_rows_flip_absorb_armed(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)", action="absorbed", armed=True),
    ])
    emit_exec_failure_signals(shared, now=NOW)
    sig = _firing(shared)[0]
    assert sig.details["absorb_armed"] is True
    assert "absorbed before reaching the user channel" in sig.body


# ── Window + hygiene ─────────────────────────────────────────────────────────

def test_old_rows_are_ignored(tmp_path):
    shared = _make_shared(tmp_path)
    old = NOW - timedelta(hours=30)
    _write_ledger(
        shared, "team_bot_a",
        [_ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)", ts=old)],
        when=old,
    )
    summary = emit_exec_failure_signals(shared, now=NOW)
    assert summary == {"bots": 0, "lines": 0, "shapes": 0, "read_errors": 0}
    assert _firing(shared) == []


def test_malformed_rows_are_skipped(tmp_path):
    shared = _make_shared(tmp_path)
    ledger_dir = shared / "team_bot_a" / "exec-failures"
    ledger_dir.mkdir(parents=True)
    f = ledger_dir / f"exec-failures-{NOW.strftime('%Y-%m-%d')}.jsonl"
    good = json.dumps(_ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"))
    f.write_text(
        "not-json\n"
        + json.dumps({"action": "unknown_action", "ts": NOW.isoformat()}) + "\n"
        + json.dumps({"action": "absorbed", "ts": "garbage", "matched_lines": ["x"]}) + "\n"
        + good + "\n",
        encoding="utf-8",
    )
    summary = emit_exec_failure_signals(shared, now=NOW)
    assert summary["lines"] == 1
    assert len(_firing(shared)) == 1


def test_naive_timestamp_row_does_not_kill_the_sweep(tmp_path):
    shared = _make_shared(tmp_path)
    row = _ledger_row("⚠️ 🛠️ Exec failed: `ls /x` (exit 1)")
    row["ts"] = "2026-08-31T11:59:00"  # no Z / offset — tolerated as UTC
    _write_ledger(shared, "team_bot_a", [row])
    summary = emit_exec_failure_signals(shared, now=NOW)
    assert summary["lines"] == 1
    assert len(_firing(shared)) == 1


def test_read_error_skips_sweep_instead_of_mass_archiving(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `ls /x` (exit 1)"),
    ])
    emit_exec_failure_signals(shared, now=NOW)
    assert len(_firing(shared)) == 1
    # Make the shard unreadable: unknown state must NOT read as "cleared".
    ledger_dir = shared / "team_bot_a" / "exec-failures"
    shard = next(ledger_dir.glob("*.jsonl"))
    shard.chmod(0o000)
    try:
        summary = emit_exec_failure_signals(shared, now=NOW)
    finally:
        shard.chmod(0o644)
    assert summary["read_errors"] == 1
    # The firing signal survives — the sweep was skipped, not run empty.
    assert len(_firing(shared)) == 1
    assert _archived(shared) == []


def test_quieted_shape_sweep_resolves(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"),
    ])
    emit_exec_failure_signals(shared, now=NOW)
    assert len(_firing(shared)) == 1
    # A day later the ledger rows have aged out of the window.
    later = NOW + timedelta(hours=25)
    emit_exec_failure_signals(shared, now=later)
    assert _firing(shared) == []
    assert len(_archived(shared)) == 1


def test_missing_shared_dir_is_quiet(tmp_path):
    summary = emit_exec_failure_signals(tmp_path / "nope", now=NOW)
    assert summary == {"bots": 0, "lines": 0, "shapes": 0, "read_errors": 0}


# ── Scheduler tick wiring (Phase 0e) ─────────────────────────────────────────

def test_audit_scheduler_tick_runs_the_sweep(tmp_path, monkeypatch):
    from evolve_admin.applications import audit_scheduler

    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _ledger_row("⚠️ 🛠️ Exec failed: `x` (exit 1)"),
    ])

    calls = []
    import evolve_admin.exec_failure_monitor as efm
    real = efm.emit_exec_failure_signals
    monkeypatch.setattr(
        efm, "emit_exec_failure_signals",
        lambda sd, **kw: calls.append(sd) or real(sd, now=NOW),
    )

    result = audit_scheduler.TickResult(started_at="t0", finished_at="t0")
    audit_scheduler._sweep_exec_failure_ledgers(shared, result)
    assert calls == [shared]
    assert result.errors == []
    assert len(_firing(shared)) == 1


def test_audit_scheduler_tick_records_monitor_error(tmp_path, monkeypatch):
    from evolve_admin.applications import audit_scheduler

    import evolve_admin.exec_failure_monitor as efm

    def boom(sd, **kw):
        raise RuntimeError("ledger scan exploded")

    monkeypatch.setattr(efm, "emit_exec_failure_signals", boom)
    result = audit_scheduler.TickResult(started_at="t0", finished_at="t0")
    audit_scheduler._sweep_exec_failure_ledgers(tmp_path, result)
    assert result.errors and result.errors[0]["stage"] == "exec_failure_monitor"
