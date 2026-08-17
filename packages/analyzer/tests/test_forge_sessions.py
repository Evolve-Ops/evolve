"""Tests for forge_sessions — annotation writer + retag helpers.

Covers the round-trip used in production:

  1. Dispatcher (bot_forge._dispatch_agent) writes an annotation via
     ``write_dispatch_annotation`` with a conservative window.
  2. Converter / usage_analytics load that day's windows via
     ``load_windows`` and pass each turn through ``retag_turn_source``.
  3. A turn that falls inside any window AND carries channel=unknown
     AND source ∈ {user, human} is rewritten with source="forge"; other
     turns pass through unchanged.

These pin the contract that prevented the 2026-05-29 atlas regression
where Forge dispatches showed up as $5+/day of "Human / channel:unknown"
on the Usage tab.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import forge_sessions as fs  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def test_forge_sessions_path_layout(tmp_path):
    p = fs.forge_sessions_path(tmp_path, "atlas", date(2026, 5, 29))
    assert p == tmp_path / "forge_sessions" / "atlas" / "2026-05-29.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# Writer
# ─────────────────────────────────────────────────────────────────────────────


def test_write_dispatch_annotation_creates_file(tmp_path):
    start = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
    out = fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="atlas",
        job_id="forge-abc123",
        suffix="",
        kind="build",
        start_ts=start,
        timeout_sec=1200,
    )
    assert out == tmp_path / "forge_sessions" / "atlas" / "2026-05-29.jsonl"
    assert out.exists()
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["schema_version"] == 2  # v2 (2026-06-03) adds optional trigger_subkind
    assert rec["job_id"] == "forge-abc123"
    # trigger_subkind is absent unless explicitly passed — kept off-disk to
    # preserve v1 reader compatibility for tooling that pinned the shape.
    assert "trigger_subkind" not in rec
    assert rec["suffix"] == ""
    assert rec["kind"] == "build"
    assert rec["bot_id"] == "atlas"
    assert rec["start_ts"] == "2026-05-29T10:00:00Z"
    # end_ts = start + timeout + 60s buffer
    assert rec["end_ts"] == "2026-05-29T10:21:00Z"


def test_write_dispatch_annotation_uses_utc_date_for_filename(tmp_path):
    # 23:30 PST on 2026-05-29 is 06:30 UTC on 2026-05-30. We write under
    # the UTC date so cross-timezone clocks don't fragment the file.
    pacific = timezone(timedelta(hours=-7))
    start_pst = datetime(2026, 5, 29, 23, 30, 0, tzinfo=pacific)
    out = fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="atlas",
        job_id="forge-x",
        suffix="-c1",
        kind="critique",
        start_ts=start_pst,
        timeout_sec=600,
    )
    assert out.name == "2026-05-30.jsonl"


def test_write_dispatch_annotation_appends(tmp_path):
    start = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
    fs.write_dispatch_annotation(
        tmp_path, "atlas", "j1", "", "build", start, 1200
    )
    fs.write_dispatch_annotation(
        tmp_path, "atlas", "j1", "-c1", "critique", start + timedelta(minutes=21), 600
    )
    out = tmp_path / "forge_sessions" / "atlas" / "2026-05-29.jsonl"
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    kinds = [json.loads(l)["kind"] for l in lines]
    assert kinds == ["build", "critique"]


def test_write_dispatch_annotation_file_is_world_readable(tmp_path):
    """The cost_event_converter runs as the bot user; it must be able to
    read what the dispatcher (evolve user) writes here. mode 0o644 is
    the contract — anything tighter would silently break the retag.
    """
    import stat
    start = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
    out = fs.write_dispatch_annotation(
        tmp_path, "atlas", "j1", "", "build", start, 60
    )
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o644


# ─────────────────────────────────────────────────────────────────────────────
# Reader
# ─────────────────────────────────────────────────────────────────────────────


def test_load_windows_quiet_bot_returns_empty(tmp_path):
    # No forge_sessions/ dir at all is normal for non-forge bots.
    assert fs.load_windows(tmp_path, "team_bot_a", date(2026, 5, 29)) == []


def test_load_windows_reads_today(tmp_path):
    start = datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)
    fs.write_dispatch_annotation(tmp_path, "atlas", "j1", "", "build", start, 1200)
    windows = fs.load_windows(tmp_path, "atlas", date(2026, 5, 29))
    assert len(windows) == 1
    assert windows[0].start == start
    assert windows[0].kind == "build"


def test_load_windows_includes_prev_day_by_default(tmp_path):
    """Cross-midnight dispatch: a build that started at 23:50 yesterday
    must still match turns that landed at 00:05 today."""
    yesterday_start = datetime(2026, 5, 28, 23, 50, 0, tzinfo=timezone.utc)
    fs.write_dispatch_annotation(
        tmp_path, "atlas", "j-cross-midnight", "", "build", yesterday_start, 1200
    )
    windows = fs.load_windows(tmp_path, "atlas", date(2026, 5, 29))
    assert len(windows) == 1
    assert windows[0].start == yesterday_start


def test_load_windows_skips_malformed_lines(tmp_path):
    p = fs.forge_sessions_path(tmp_path, "atlas", date(2026, 5, 29))
    p.parent.mkdir(parents=True)
    p.write_text(
        "not-json\n"
        '{"schema_version":1,"job_id":"good","start_ts":"2026-05-29T10:00:00Z","end_ts":"2026-05-29T10:21:00Z"}\n'
        '{"schema_version":1,"start_ts":"bad-ts","end_ts":"also-bad"}\n'
    )
    windows = fs.load_windows(tmp_path, "atlas", date(2026, 5, 29), include_prev_day=False)
    assert len(windows) == 1
    assert windows[0].job_id == "good"


# ─────────────────────────────────────────────────────────────────────────────
# Matcher
# ─────────────────────────────────────────────────────────────────────────────


def _window(start_iso, end_iso, *, kind="build"):
    return fs.ForgeWindow(
        start=fs._parse_iso(start_iso),
        end=fs._parse_iso(end_iso),
        job_id="j",
        kind=kind,
        suffix="",
    )


def test_is_forge_turn_inside_window_with_user_source():
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    assert fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "unknown", "user")
    assert fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "unknown", "human")


def test_is_forge_turn_outside_window():
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    # Before window
    assert not fs.is_forge_turn(windows, "2026-05-29T09:59:59Z", "unknown", "user")
    # After window
    assert not fs.is_forge_turn(windows, "2026-05-29T10:21:01Z", "unknown", "user")


def test_is_forge_turn_rejects_explicit_channel():
    """Real Telegram/Slack/Discord turns must never be retagged even if
    their timestamp happens to overlap a forge window. The channel filter
    is the safety net."""
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    assert not fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "telegram", "user")
    assert not fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "slack", "user")


def test_is_forge_turn_rejects_non_retaggable_sources():
    """Heartbeat / cron / subagent paths carry their own source values;
    we must not overwrite them even inside a forge window."""
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    assert not fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "unknown", "heartbeat")
    assert not fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "unknown", "cron")
    assert not fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "unknown", "subagent")


def test_is_forge_turn_handles_null_channel():
    """TurnObserver's evolve-shared output sometimes emits channel=null
    (not 'unknown') for local-agent turns. Match those too."""
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    assert fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", None, "user")
    assert fs.is_forge_turn(windows, "2026-05-29T10:05:00Z", "", "user")


def test_is_forge_turn_no_windows_returns_false():
    assert not fs.is_forge_turn([], "2026-05-29T10:05:00Z", "unknown", "user")


def test_retag_turn_source_matches():
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    turn = {
        "ts": "2026-05-29T10:05:00Z",
        "channel": "unknown",
        "source": "user",
        "cost": 1.04,
    }
    out = fs.retag_turn_source(turn, windows)
    assert out["source"] == "forge"
    # Other fields intact
    assert out["cost"] == 1.04
    assert out["channel"] == "unknown"
    # Original turn unchanged (avoid silently mutating caller's list)
    assert turn["source"] == "user"


def test_retag_turn_source_passes_through_when_no_match():
    windows = [_window("2026-05-29T10:00:00Z", "2026-05-29T10:21:00Z")]
    turn = {"ts": "2026-05-29T09:00:00Z", "channel": "telegram", "source": "user"}
    out = fs.retag_turn_source(turn, windows)
    # No-match path returns the original object (no copy)
    assert out is turn
    assert out["source"] == "user"


# ─────────────────────────────────────────────────────────────────────────────
# trigger_subkind (2026-06-03) — propagates operator-confirmed-install tag
# through write → read → retag so spend_alert can honour daily-cap exemption.
# ─────────────────────────────────────────────────────────────────────────────


def test_write_dispatch_annotation_records_trigger_subkind(tmp_path):
    start = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)
    out = fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="team_bot_c",
        job_id="forge-conf-1",
        suffix="",
        kind="build",
        start_ts=start,
        timeout_sec=1200,
        trigger_subkind="operator_confirmed_install",
    )
    rec = json.loads(out.read_text().strip())
    assert rec["trigger_subkind"] == "operator_confirmed_install"


def test_load_windows_propagates_trigger_subkind(tmp_path):
    start = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)
    fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="team_bot_c",
        job_id="forge-conf-1",
        suffix="",
        kind="build",
        start_ts=start,
        timeout_sec=1200,
        trigger_subkind="operator_confirmed_install",
    )
    windows = fs.load_windows(tmp_path, "team_bot_c", date(2026, 6, 3), include_prev_day=False)
    assert len(windows) == 1
    assert windows[0].trigger_subkind == "operator_confirmed_install"


def test_load_windows_accepts_v1_records_without_subkind(tmp_path):
    """A v1 annotation (no trigger_subkind field) still loads cleanly."""
    out = fs.forge_sessions_path(tmp_path, "atlas", date(2026, 6, 3))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '{"schema_version":1,"job_id":"v1job","kind":"build","suffix":"",'
        '"bot_id":"atlas","start_ts":"2026-06-03T10:00:00Z","end_ts":"2026-06-03T10:21:00Z"}\n'
    )
    windows = fs.load_windows(tmp_path, "atlas", date(2026, 6, 3), include_prev_day=False)
    assert len(windows) == 1
    assert windows[0].trigger_subkind is None


def test_retag_turn_source_copies_subkind_when_window_carries_it():
    windows = [
        fs.ForgeWindow(
            start=fs._parse_iso("2026-06-03T10:00:00Z"),
            end=fs._parse_iso("2026-06-03T10:21:00Z"),
            job_id="j",
            kind="build",
            suffix="",
            trigger_subkind="operator_confirmed_install",
        )
    ]
    turn = {"ts": "2026-06-03T10:05:00Z", "channel": "unknown", "source": "user"}
    out = fs.retag_turn_source(turn, windows)
    assert out["source"] == "forge"
    assert out["forge_subkind"] == "operator_confirmed_install"


def test_retag_turn_source_does_not_set_subkind_when_window_lacks_it():
    """A vanilla critique/refine window leaves forge_subkind off the turn."""
    windows = [_window("2026-06-03T10:00:00Z", "2026-06-03T10:21:00Z")]
    turn = {"ts": "2026-06-03T10:05:00Z", "channel": "unknown", "source": "user"}
    out = fs.retag_turn_source(turn, windows)
    assert out["source"] == "forge"
    assert "forge_subkind" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Integration with cost_event_converter
# ─────────────────────────────────────────────────────────────────────────────


def test_cost_event_converter_retag_forge(tmp_path, monkeypatch):
    """End-to-end: a forge dispatch annotation + a matching turn record
    produces a cost_event with trigger_kind="forge", not "user_turn"."""
    import cost_event_converter as cec
    monkeypatch.setattr(cec, "_bot_home", lambda bid: tmp_path / "bots" / bid)

    target = date(2026, 5, 29)
    # Stamp a forge dispatch window at 10:00 UTC (1200s timeout).
    fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="atlas",
        job_id="forge-abc",
        suffix="",
        kind="build",
        start_ts=datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc),
        timeout_sec=1200,
    )

    # Write a matching turn record (inside the window, channel=unknown,
    # source=user — exactly how OC writes a --local --agent main turn).
    src = (
        tmp_path / "bots" / "atlas" / ".openclaw" / "workspace" / "memory"
        / f"turns-{target.isoformat()}.jsonl"
    )
    src.parent.mkdir(parents=True)
    # Matches the OC turn-collector schema (source="human", channel="unknown"
    # for a `--local --agent main` dispatch).
    forge_turn = {
        "ts": "2026-05-29T10:05:00.000000+00:00",
        "channel": "unknown",
        "user_id": "unknown",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "cost": 1.04,
        "input_tokens": 5,
        "output_tokens": 200,
        "cache_read_tokens": 10000,
        "cache_write_tokens": 5000,
        "session_id": "da1e3dc6-4c33-4ec5-950e-4648aed612dc",
    }
    # Plus a real human turn outside any window — must NOT be retagged.
    human_turn = dict(forge_turn)
    human_turn["ts"] = "2026-05-29T11:00:00.000000+00:00"
    human_turn["channel"] = "telegram"
    human_turn["session_id"] = "real-human-session"
    with src.open("w") as f:
        f.write(json.dumps(forge_turn) + "\n")
        f.write(json.dumps(human_turn) + "\n")

    report = cec.convert_day("atlas", target, tmp_path)
    assert report["written"] == 2

    out = tmp_path / "annotations" / "atlas" / f"cost_events-{target.isoformat()}.jsonl"
    events = [json.loads(l) for l in out.read_text().strip().splitlines()]
    events_by_session = {e["session_id"]: e for e in events}
    assert events_by_session["da1e3dc6-4c33-4ec5-950e-4648aed612dc"]["trigger_kind"] == "forge"
    # No subkind: the dispatch annotation didn't opt one in.
    assert events_by_session["da1e3dc6-4c33-4ec5-950e-4648aed612dc"]["trigger_subkind"] is None
    # Real Telegram turn from the same bot in the same window stays human.
    assert events_by_session["real-human-session"]["trigger_kind"] == "user_turn"
    assert events_by_session["real-human-session"]["trigger_subkind"] is None


def test_cost_event_converter_propagates_trigger_subkind(tmp_path, monkeypatch):
    """End-to-end (2026-06-03): an operator-confirmed forge dispatch annotation
    + a matching turn → cost_event with both trigger_kind=forge AND
    trigger_subkind=operator_confirmed_install. The exemption gate in
    spend_alert reads the subkind field downstream.
    """
    import cost_event_converter as cec
    monkeypatch.setattr(cec, "_bot_home", lambda bid: tmp_path / "bots" / bid)

    target = date(2026, 6, 3)
    fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="team_bot_c",
        job_id="forge-conf-1",
        suffix="",
        kind="build",
        start_ts=datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc),
        timeout_sec=1200,
        trigger_subkind="operator_confirmed_install",
    )

    src = (
        tmp_path / "bots" / "team_bot_c" / ".openclaw" / "workspace" / "memory"
        / f"turns-{target.isoformat()}.jsonl"
    )
    src.parent.mkdir(parents=True)
    forge_turn = {
        "ts": "2026-06-03T10:05:00.000000+00:00",
        "channel": "unknown",
        "user_id": "unknown",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "cost": 5.0,
        "input_tokens": 10000,
        "output_tokens": 1000,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 200,
        "session_id": "confirmed-install-session-1",
    }
    with src.open("w") as f:
        f.write(json.dumps(forge_turn) + "\n")

    cec.convert_day("team_bot_c", target, tmp_path)
    out = tmp_path / "annotations" / "team_bot_c" / f"cost_events-{target.isoformat()}.jsonl"
    events = [json.loads(l) for l in out.read_text().strip().splitlines()]
    assert len(events) == 1
    e = events[0]
    assert e["trigger_kind"] == "forge"
    assert e["trigger_subkind"] == "operator_confirmed_install"


def test_cost_event_converter_retag_via_evolve_src_path(tmp_path, monkeypatch):
    """Atlas (the bot that surfaced this bug 2026-05-29) doesn't have the
    OC turn-collector deployed yet, so its data comes through the evolve
    shared-turns fallback — TurnObserver writes ``source: "user"`` (not
    "human") on that path. The retag must work there too.
    """
    import cost_event_converter as cec
    monkeypatch.setattr(cec, "_bot_home", lambda bid: tmp_path / "bots" / bid)

    target = date(2026, 5, 29)
    fs.write_dispatch_annotation(
        shared_dir=tmp_path,
        bot_id="atlas",
        job_id="forge-abc",
        suffix="",
        kind="build",
        start_ts=datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc),
        timeout_sec=1200,
    )

    # Note the evolve_src path — under {shared_dir}/{bot}/turns/, with
    # source="user" and a separate provider field (TurnObserver schema).
    evolve_src = tmp_path / "atlas" / "turns" / f"turns-{target.isoformat()}.jsonl"
    evolve_src.parent.mkdir(parents=True)
    evolve_src.write_text(json.dumps({
        "ts": "2026-05-29T10:05:00.000000+00:00",
        "channel": "unknown",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "source": "user",
        "cost": 1.04,
        "input_tokens": 5,
        "output_tokens": 200,
        "cache_read_tokens": 10000,
        "cache_write_tokens": 5000,
        "session_id": "atlas-forge-session",
    }) + "\n")

    cec.convert_day("atlas", target, tmp_path)
    out = tmp_path / "annotations" / "atlas" / f"cost_events-{target.isoformat()}.jsonl"
    events = [json.loads(l) for l in out.read_text().strip().splitlines()]
    assert len(events) == 1
    assert events[0]["trigger_kind"] == "forge"


def test_cost_event_converter_no_forge_annotations(tmp_path, monkeypatch):
    """Bots that never ran a forge dispatch — most of the pod — have no
    annotation files, and the existing user_turn classification stands."""
    import cost_event_converter as cec
    monkeypatch.setattr(cec, "_bot_home", lambda bid: tmp_path / "bots" / bid)

    target = date(2026, 5, 29)
    src = (
        tmp_path / "bots" / "team_bot_a" / ".openclaw" / "workspace" / "memory"
        / f"turns-{target.isoformat()}.jsonl"
    )
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({
        "ts": "2026-05-29T10:05:00.000000+00:00",
        "channel": "slack",
        "model": "anthropic/claude-sonnet-4-6",
        "source": "human",
        "cost": 0.05,
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "session_id": "team_bot_a-session",
    }) + "\n")

    cec.convert_day("team_bot_a", target, tmp_path)
    out = tmp_path / "annotations" / "team_bot_a" / f"cost_events-{target.isoformat()}.jsonl"
    events = [json.loads(l) for l in out.read_text().strip().splitlines()]
    assert len(events) == 1
    assert events[0]["trigger_kind"] == "user_turn"
