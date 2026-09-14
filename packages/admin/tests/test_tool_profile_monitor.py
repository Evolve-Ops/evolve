"""tests/test_tool_profile_monitor.py — out-of-profile tool-call aggregation.

Design: internal/design-pa-context-economy-2026-08-31.md §3 CE-2. The
plugin-side tool-profile filter trims a background / one-shot session's Evolve
tool surface and ledgers every refused call; this producer folds those ledgers
into dedup'd Signals so a profile that is too narrow becomes visible as
EVIDENCE rather than as an unexplained model retry. These tests pin:

  - repeated refusals of the same (bot, profile, tool) fold into ONE Signal
    whose details carry the in-window count and the session kinds seen
  - distinct tools, distinct profiles and distinct bots never share a Signal
  - the ledger is read from ``turns/`` (where the plugin writes it) and the
    file name is UTC-dated — the shard set is derived from the window, never
    from local time
  - a combination absent from the window sweep-resolves; a ledger READ error
    skips the sweep entirely (unknown state is not "cleared")
  - rows older than the window are ignored, and malformed rows (bad JSON, a
    missing tool name, a naive timestamp) never kill the sweep
  - the ledger carries its own retention: shards older than RETENTION_DAYS are
    pruned by this monitor, and only exactly-named dated shards ever are
  - the hourly audit-scheduler tick runs the sweep and records an error
    without failing the tick
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

from evolve_admin.tool_profile_monitor import (  # noqa: E402
    LEDGER_PREFIX,
    LEDGER_SUBDIR,
    PRODUCER,
    RETENTION_DAYS,
    TYPE_REFUSAL,
    WINDOW_HOURS,
    _window_dates,
    emit_tool_profile_signals,
)

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _make_shared(tmp_path: Path) -> Path:
    shared = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (shared / "signals" / sub).mkdir(parents=True)
    return shared


def _row(
    tool: str,
    *,
    profile: str = "no_live_speaker",
    kind: str = "scheduled",
    ts: datetime = NOW,
    session_key: str = "agent:main:cron:0000",
) -> dict:
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "bot_id": "unused-here",
        "tool_name": tool,
        "profile": profile,
        "session_kind": kind,
        "session_key": session_key,
        "session_id": "sid-1",
    }


def _write_ledger(
    shared: Path, bot: str, rows: "list[dict]", *, when: datetime = NOW,
    extra_lines: "list[str] | None" = None,
) -> Path:
    ledger_dir = shared / bot / LEDGER_SUBDIR
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{LEDGER_PREFIX}{when.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        for line in extra_lines or []:
            fh.write(line + "\n")
    return path


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


# ── Aggregation ──────────────────────────────────────────────────────────────

def test_repeated_refusals_of_one_tool_fold_into_one_signal(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _row("roster_block"),
        _row("roster_block", kind="oneshot"),
        _row("roster_block"),
    ])
    summary = emit_tool_profile_signals(shared, now=NOW)
    assert summary == {"bots": 1, "refusals": 3, "combinations": 1,
                       "read_errors": 0, "pruned": 0}
    sigs = _firing(shared)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == TYPE_REFUSAL
    assert sig.scope == "bot" and sig.bot_id == "team_bot_a"
    assert sig.details["tool"] == "roster_block"
    assert sig.details["profile"] == "no_live_speaker"
    assert sig.details["count_in_window"] == 3
    assert sig.details["session_kinds"] == ["oneshot", "scheduled"]
    # The Signal must say WHERE to widen the profile — the whole point of it.
    assert sig.details["widen_at"].endswith("ToolProfiles.ts")
    assert "ToolProfiles.ts" in sig.body


def test_distinct_tools_profiles_and_bots_never_share_a_signal(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [
        _row("roster_block"),
        _row("session_set_tier"),
        _row("roster_block", profile="evolve_dispatch", kind="evolve_internal"),
    ])
    _write_ledger(shared, "team_bot_b", [_row("roster_block")])
    summary = emit_tool_profile_signals(shared, now=NOW)
    assert summary["bots"] == 2 and summary["combinations"] == 4
    keys = {
        (s.bot_id, s.details["profile"], s.details["tool"]) for s in _firing(shared)
    }
    assert keys == {
        ("team_bot_a", "no_live_speaker", "roster_block"),
        ("team_bot_a", "no_live_speaker", "session_set_tier"),
        ("team_bot_a", "evolve_dispatch", "roster_block"),
        ("team_bot_b", "no_live_speaker", "roster_block"),
    }


def test_ledger_is_read_from_the_turns_dir_the_plugin_writes(tmp_path):
    """The ledger lives beside the prefix-hash ledger and the tool footprint —
    the one per-bot dir the gateway already writes and evolve already reads."""
    shared = _make_shared(tmp_path)
    path = _write_ledger(shared, "team_bot_a", [_row("roster_block")])
    assert path.parent.name == "turns"
    assert path.name == f"tool-profile-refusals-{NOW:%Y-%m-%d}.jsonl"
    assert emit_tool_profile_signals(shared, now=NOW)["combinations"] == 1
    # A row in some OTHER per-bot subdir is not this producer's business.
    other = shared / "team_bot_a" / "exec-failures"
    other.mkdir(parents=True, exist_ok=True)
    (other / f"{LEDGER_PREFIX}{NOW:%Y-%m-%d}.jsonl").write_text(
        json.dumps(_row("gmail_send")) + "\n", encoding="utf-8")
    assert emit_tool_profile_signals(shared, now=NOW)["combinations"] == 1


def test_window_dates_are_utc_shards_derived_from_the_window():
    dates = _window_dates(NOW)
    assert f"{NOW:%Y-%m-%d}" in dates
    # Enough shards to cover the window plus the boundary day either side.
    assert len(dates) >= int(WINDOW_HOURS // 24) + 2
    # Derived from the passed instant, not from the local clock.
    assert _window_dates(NOW - timedelta(days=30)) != dates


# ── Honesty ──────────────────────────────────────────────────────────────────

def test_rows_outside_the_window_are_ignored_and_then_sweep_resolve(tmp_path):
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [_row("roster_block")])
    emit_tool_profile_signals(shared, now=NOW)
    assert len(_firing(shared)) == 1
    later = NOW + timedelta(hours=WINDOW_HOURS + 1)
    emit_tool_profile_signals(shared, now=later)
    assert _firing(shared) == []
    assert len(_archived(shared)) == 1


def test_a_ledger_read_error_skips_the_sweep(tmp_path):
    """Unknown state is not "the refusals stopped" — a Signal we could not
    re-observe must never auto-archive."""
    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [_row("roster_block")])
    emit_tool_profile_signals(shared, now=NOW)
    assert len(_firing(shared)) == 1

    later = NOW + timedelta(hours=WINDOW_HOURS + 1)
    # Replace the shard the later run WOULD read with a directory: open() fails
    # with an OSError that is not FileNotFoundError.
    ledger_dir = shared / "team_bot_a" / LEDGER_SUBDIR
    (ledger_dir / f"{LEDGER_PREFIX}{later:%Y-%m-%d}.jsonl").mkdir()
    summary = emit_tool_profile_signals(shared, now=later)
    assert summary["read_errors"] >= 1
    assert len(_firing(shared)) == 1, "an unreadable ledger must not archive a live Signal"


def test_malformed_rows_never_kill_the_sweep(tmp_path):
    shared = _make_shared(tmp_path)
    naive = dict(_row("gmail_send"))
    naive["ts"] = NOW.replace(tzinfo=None).isoformat()  # no offset
    nameless = dict(_row("x"))
    nameless.pop("tool_name")
    _write_ledger(
        shared, "team_bot_a",
        [_row("roster_block"), naive, nameless, {"ts": "not-a-time", "tool_name": "y"}],
        extra_lines=["", "{not json", "[]"],
    )
    summary = emit_tool_profile_signals(shared, now=NOW)
    # The good row and the naive-timestamp row both count; the rest are dropped.
    assert summary["combinations"] == 2 and summary["read_errors"] == 0
    assert {s.details["tool"] for s in _firing(shared)} == {"roster_block", "gmail_send"}


def test_missing_shared_dir_is_quiet(tmp_path):
    assert emit_tool_profile_signals(tmp_path / "nope", now=NOW) == {
        "bots": 0, "refusals": 0, "combinations": 0, "read_errors": 0, "pruned": 0,
    }


def test_a_pod_with_no_refusals_fires_nothing(tmp_path):
    shared = _make_shared(tmp_path)
    (shared / "team_bot_a" / LEDGER_SUBDIR).mkdir(parents=True)
    assert emit_tool_profile_signals(shared, now=NOW)["combinations"] == 0
    assert _firing(shared) == []


# ── Retention ────────────────────────────────────────────────────────────────

def test_old_shards_are_pruned_and_nothing_else_in_turns_is_touched(tmp_path):
    """A new accumulating write path carries its own retention. The prune is
    exact-name-matched, so the prefix-hash ledger and the tool footprint that
    share this dir are never at risk."""
    shared = _make_shared(tmp_path)
    stale = NOW - timedelta(days=RETENTION_DAYS + 1)
    _write_ledger(shared, "team_bot_a", [_row("roster_block", ts=stale)], when=stale)
    _write_ledger(shared, "team_bot_a", [_row("roster_block")])
    turns = shared / "team_bot_a" / LEDGER_SUBDIR
    neighbours = {
        f"prefix-hashes-{stale:%Y-%m-%d}.jsonl": "{}\n",
        "context-footprint.json": "{}",
        f"{LEDGER_PREFIX}not-a-date.jsonl": "{}\n",
    }
    for name, body in neighbours.items():
        (turns / name).write_text(body, encoding="utf-8")

    summary = emit_tool_profile_signals(shared, now=NOW)
    assert summary["pruned"] == 1
    left = {p.name for p in turns.iterdir()}
    assert f"{LEDGER_PREFIX}{stale:%Y-%m-%d}.jsonl" not in left
    assert f"{LEDGER_PREFIX}{NOW:%Y-%m-%d}.jsonl" in left
    assert set(neighbours) <= left, "the prune must not touch its neighbours"


def test_a_read_error_also_skips_the_prune(tmp_path):
    """A shard we could not read is not a shard we have finished with."""
    shared = _make_shared(tmp_path)
    stale = NOW - timedelta(days=RETENTION_DAYS + 1)
    _write_ledger(shared, "team_bot_a", [_row("roster_block", ts=stale)], when=stale)
    turns = shared / "team_bot_a" / LEDGER_SUBDIR
    (turns / f"{LEDGER_PREFIX}{NOW:%Y-%m-%d}.jsonl").mkdir()
    summary = emit_tool_profile_signals(shared, now=NOW)
    assert summary["read_errors"] >= 1 and summary["pruned"] == 0
    assert (turns / f"{LEDGER_PREFIX}{stale:%Y-%m-%d}.jsonl").exists()


# ── Scheduler tick wiring ────────────────────────────────────────────────────

def test_audit_scheduler_tick_runs_the_sweep(tmp_path, monkeypatch):
    from evolve_admin.applications import audit_scheduler

    shared = _make_shared(tmp_path)
    _write_ledger(shared, "team_bot_a", [_row("roster_block")])

    calls = []
    import evolve_admin.tool_profile_monitor as tpm
    real = tpm.emit_tool_profile_signals
    monkeypatch.setattr(
        tpm, "emit_tool_profile_signals",
        lambda sd, **kw: calls.append(sd) or real(sd, now=NOW),
    )
    result = audit_scheduler.TickResult(started_at="t0", finished_at="t0")
    audit_scheduler._sweep_tool_profile_refusals(shared, result)
    assert calls == [shared]
    assert result.errors == []
    assert len(_firing(shared)) == 1


def test_audit_scheduler_tick_records_monitor_error(tmp_path, monkeypatch):
    from evolve_admin.applications import audit_scheduler
    import evolve_admin.tool_profile_monitor as tpm

    def boom(sd, **kw):
        raise RuntimeError("ledger scan exploded")

    monkeypatch.setattr(tpm, "emit_tool_profile_signals", boom)
    result = audit_scheduler.TickResult(started_at="t0", finished_at="t0")
    audit_scheduler._sweep_tool_profile_refusals(tmp_path, result)
    assert result.errors and result.errors[0]["stage"] == "tool_profile_monitor"
