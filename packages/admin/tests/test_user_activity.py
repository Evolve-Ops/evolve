"""Unit tests for evolve_admin.user_activity.

Covers per-user aggregation of turn rollup records — what the Users
page reads to populate the "Last seen" column.

Spec: docs/spec-user-roster-and-roles-2026-06-07.md §D.2 (post-deploy
operator feedback).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import user_activity as ua  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


def _write_turns(shared: Path, bot_id: str, date_str: str,
                 records: list[dict]) -> None:
    """Write records to ``{shared}/<bot>/turns/turns-<date>.jsonl``."""
    d = shared / bot_id / "turns"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"turns-{date_str}.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(ts: str, user_id: str | None, channel: str = "telegram",
         **extra) -> dict:
    """Build a turn record matching the writeTurnToShared shape."""
    return {
        "ts": ts,
        "instance": "atlas",
        "model": "claude-sonnet-4-6",
        "channel": channel,
        "user_id": user_id,
        "session_id": "test-session",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost": 0.001,
        **extra,
    }


# ── Empty / missing inputs ──────────────────────────────────────────────


def test_missing_turns_dir_returns_empty(shared):
    result = ua.aggregate(shared, "atlas")
    assert result == {}


def test_empty_turns_file_returns_empty(shared):
    _write_turns(shared, "atlas", "2026-06-08", [])
    result = ua.aggregate(shared, "atlas")
    assert result == {}


# ── Single-user aggregation ─────────────────────────────────────────────


def test_single_record_populates_one_user(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T11:30:00Z", "1260193629", "telegram"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert "telegram:1260193629" in result
    entry = result["telegram:1260193629"]
    assert entry["last_ts"] == "2026-06-08T11:30:00Z"
    assert entry["turns_7d"] == 1
    assert entry["channels"] == ["telegram"]


def test_multiple_records_same_user_aggregate_correctly(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T10:00:00Z", "1260193629"),
        _rec("2026-06-08T11:30:00Z", "1260193629"),
        _rec("2026-06-08T11:45:00Z", "1260193629"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    entry = result["telegram:1260193629"]
    assert entry["turns_7d"] == 3
    # last_ts is the newest
    assert entry["last_ts"] == "2026-06-08T11:45:00Z"


# ── Window boundaries ──────────────────────────────────────────────────


def test_records_older_than_7d_excluded_from_turns_7d_but_in_last_ts(shared):
    """A 20-day-old record sets last_ts (within 30d window) but isn't
    counted in turns_7d. Lets the operator see "last seen 3w ago"
    accurately while turns_7d stays meaningful."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-05-19", [
        _rec("2026-05-19T08:00:00Z", "1260193629"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    entry = result["telegram:1260193629"]
    assert entry["turns_7d"] == 0
    assert entry["last_ts"] == "2026-05-19T08:00:00Z"


def test_records_older_than_30d_excluded_entirely(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-04-01", [
        _rec("2026-04-01T12:00:00Z", "1260193629"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert "telegram:1260193629" not in result


def test_smaller_window_param_tightens_horizon(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-05-15", [
        _rec("2026-05-15T12:00:00Z", "1260193629"),
    ])
    # 30-day window includes May 15
    assert "telegram:1260193629" in ua.aggregate(
        shared, "atlas", now=now)
    # 10-day window excludes May 15
    assert "telegram:1260193629" not in ua.aggregate(
        shared, "atlas", window_days=10, now=now)


# ── user_id=null filtering ──────────────────────────────────────────────


def test_null_user_id_excluded(shared):
    """Auto-source heartbeats and unenriched turns have user_id=null —
    they shouldn't appear in per-user activity."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T11:00:00Z", None, channel="heartbeat"),
        _rec("2026-06-08T11:30:00Z", "1260193629", channel="telegram"),
        _rec("2026-06-08T11:45:00Z", None, channel="telegram"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert list(result.keys()) == ["telegram:1260193629"]
    assert result["telegram:1260193629"]["turns_7d"] == 1


def test_missing_user_id_field_excluded(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    # Pre-D.1 records sometimes don't have the field at all.
    _write_turns(shared, "atlas", "2026-06-08", [
        {"ts": "2026-06-08T11:00:00Z", "channel": "telegram"},
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert result == {}


# ── Multi-user, multi-channel ──────────────────────────────────────────


def test_multiple_users_aggregate_independently(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T11:00:00Z", "111", "telegram"),
        _rec("2026-06-08T11:30:00Z", "222", "telegram"),
        _rec("2026-06-08T11:45:00Z", "111", "telegram"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert set(result.keys()) == {"telegram:111", "telegram:222"}
    assert result["telegram:111"]["turns_7d"] == 2
    assert result["telegram:222"]["turns_7d"] == 1


def test_same_user_on_different_channels_keys_separately(shared):
    """A user pairing the same id on multiple channels (rare on the
    deployed pod but plausible for cross-platform users) gets distinct
    activity buckets — the platform prefix is part of the key."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T11:00:00Z", "U0XXX", "slack"),
        _rec("2026-06-08T11:30:00Z", "U0XXX", "telegram"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert "slack:U0XXX" in result
    assert "telegram:U0XXX" in result
    assert result["slack:U0XXX"]["turns_7d"] == 1
    assert result["telegram:U0XXX"]["turns_7d"] == 1


# ── Multi-file aggregation ──────────────────────────────────────────────


def test_aggregates_across_multiple_day_files(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-06", [
        _rec("2026-06-06T08:00:00Z", "111"),
    ])
    _write_turns(shared, "atlas", "2026-06-07", [
        _rec("2026-06-07T08:00:00Z", "111"),
    ])
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111"),
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    entry = result["telegram:111"]
    assert entry["turns_7d"] == 3
    assert entry["last_ts"] == "2026-06-08T08:00:00Z"


# ── Robustness ─────────────────────────────────────────────────────────


def test_malformed_jsonl_line_skipped(shared):
    """A corrupt line doesn't kill the rest of the file."""
    d = shared / "atlas" / "turns"
    d.mkdir(parents=True)
    (d / "turns-2026-06-08.jsonl").write_text(
        "not valid json\n"
        + json.dumps(_rec("2026-06-08T11:00:00Z", "111")) + "\n"
    )
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    result = ua.aggregate(shared, "atlas", now=now)
    assert "telegram:111" in result
    assert result["telegram:111"]["turns_7d"] == 1


def test_blank_lines_skipped(shared):
    d = shared / "atlas" / "turns"
    d.mkdir(parents=True)
    (d / "turns-2026-06-08.jsonl").write_text(
        "\n\n"
        + json.dumps(_rec("2026-06-08T11:00:00Z", "111")) + "\n"
        + "\n"
    )
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    result = ua.aggregate(shared, "atlas", now=now)
    assert result["telegram:111"]["turns_7d"] == 1


def test_malformed_filename_skipped(shared):
    """A junk filename in turns/ (e.g. .DS_Store or backup) doesn't crash."""
    d = shared / "atlas" / "turns"
    d.mkdir(parents=True)
    (d / "turns-not-a-date.jsonl").write_text(
        json.dumps(_rec("2026-06-08T11:00:00Z", "111")) + "\n"
    )
    # Should not raise; should return empty since the only file has a bad name.
    result = ua.aggregate(shared, "atlas")
    assert result == {}


# ── lookup() convenience ────────────────────────────────────────────────


def test_lookup_returns_record_when_present(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T11:00:00Z", "111"),
    ])
    activity = ua.aggregate(shared, "atlas", now=now)
    rec = ua.lookup(activity, "telegram", "111")
    assert rec is not None
    assert rec["turns_7d"] == 1


def test_lookup_returns_none_for_unknown(shared):
    assert ua.lookup({}, "telegram", "nobody") is None


# ── Phase D.3 — extended aggregation (turns_30d, cost_30d, sessions_30d,
#   daily_buckets) ─────────────────────────────────────────────────────


def test_turns_30d_counts_window(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    # Three turns: today, 3 days ago, 20 days ago — all in 30d window.
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111"),
    ])
    _write_turns(shared, "atlas", "2026-06-05", [
        _rec("2026-06-05T08:00:00Z", "111"),
    ])
    _write_turns(shared, "atlas", "2026-05-19", [
        _rec("2026-05-19T08:00:00Z", "111"),
    ])
    entry = ua.aggregate(shared, "atlas", now=now)["telegram:111"]
    assert entry["turns_30d"] == 3
    assert entry["turns_7d"] == 2  # 20d-ago turn not in 7d window


def test_cost_30d_sums_across_records(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111", cost=0.123),
        _rec("2026-06-08T09:00:00Z", "111", cost=0.456),
    ])
    entry = ua.aggregate(shared, "atlas", now=now)["telegram:111"]
    assert entry["cost_30d"] == 0.579


def test_cost_30d_ignores_missing_or_zero_or_negative(shared):
    """Records without cost (older writers) or with non-positive cost
    don't contribute. Defensive against bad writer output."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111"),  # no cost field at all
        _rec("2026-06-08T09:00:00Z", "111", cost=0),
        _rec("2026-06-08T10:00:00Z", "111", cost=-1.0),
        _rec("2026-06-08T11:00:00Z", "111", cost=0.5),
    ])
    # Drop the cost field from the first record entirely.
    p = shared / "atlas" / "turns" / "turns-2026-06-08.jsonl"
    lines = p.read_text().splitlines()
    import json as _json
    line0 = _json.loads(lines[0])
    line0.pop("cost", None)
    lines[0] = _json.dumps(line0)
    p.write_text("\n".join(lines) + "\n")
    entry = ua.aggregate(shared, "atlas", now=now)["telegram:111"]
    assert entry["cost_30d"] == 0.5


def test_sessions_30d_dedups_by_session_id(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111", session_id="s1"),
        _rec("2026-06-08T08:30:00Z", "111", session_id="s1"),  # same session
        _rec("2026-06-08T09:00:00Z", "111", session_id="s2"),
    ])
    entry = ua.aggregate(shared, "atlas", now=now)["telegram:111"]
    assert entry["sessions_30d"] == 2
    assert entry["turns_30d"] == 3


def test_daily_buckets_layout_today_first(shared):
    """daily_buckets[0] = today, [1] = yesterday, ... — that's the
    layout the sparkline renderer expects."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111"),
        _rec("2026-06-08T09:00:00Z", "111"),
    ])
    _write_turns(shared, "atlas", "2026-06-07", [
        _rec("2026-06-07T08:00:00Z", "111"),
    ])
    entry = ua.aggregate(shared, "atlas", now=now)["telegram:111"]
    assert entry["daily_buckets"][0] == 2  # today
    assert entry["daily_buckets"][1] == 1  # yesterday
    assert entry["daily_buckets"][2] == 0  # 2 days ago
    assert len(entry["daily_buckets"]) == 30  # default window


def test_daily_buckets_respects_window_days(shared):
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        _rec("2026-06-08T08:00:00Z", "111"),
    ])
    entry = ua.aggregate(
        shared, "atlas", window_days=7, now=now)["telegram:111"]
    assert len(entry["daily_buckets"]) == 7
    assert entry["daily_buckets"][0] == 1



# ── G.6 — plugin TurnObserver format (channel field = runtime context) ─


def test_aggregate_handles_slack_d_channel_records(shared):
    """Plugin TurnObserver writes Slack DM turns with ``channel="D..."``
    and ``user_id=null``. G.6 makes aggregate's identity inference
    bucket those under ``slack:D...`` instead of skipping them."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "team_bot_a", "2026-06-08", [
        {"ts": "2026-06-08T08:00:00Z", "channel": "D0AN2MRGS0N",
         "user_id": None, "session_id": "s1", "source": "user",
         "cost": 0.05},
    ])
    result = ua.aggregate(shared, "team_bot_a", now=now)
    assert "slack:D0AN2MRGS0N" in result
    entry = result["slack:D0AN2MRGS0N"]
    assert entry["turns_30d"] == 1
    assert entry["cost_30d"] == 0.05


def test_aggregate_handles_telegram_numeric_channel(shared):
    """Plugin TurnObserver writes Telegram DM turns with numeric
    channel field. _extract_identity infers platform from the digit
    shape."""
    now = _dt.datetime(2026, 6, 8, 12, 0, tzinfo=_dt.timezone.utc)
    _write_turns(shared, "atlas", "2026-06-08", [
        {"ts": "2026-06-08T08:00:00Z", "channel": "1260193629",
         "user_id": None, "session_id": "s1", "source": "user"},
    ])
    result = ua.aggregate(shared, "atlas", now=now)
    assert "telegram:1260193629" in result


def test_rewrite_slack_dm_keys_maps_d_to_u(monkeypatch):
    """rewrite_slack_dm_keys translates ``slack:D0...`` keys to
    ``slack:U0...`` so the GET endpoint joins on the approved list's
    U-id."""
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: (
            "U0REAL_USER" if channel_id == "D0AMYBZ4RM1" else None))
    activity = {
        "slack:D0AMYBZ4RM1": {
            "last_ts": "2026-06-08T08:00:00Z",
            "turns_7d": 3, "turns_30d": 5, "cost_30d": 0.5,
            "sessions_30d": 2, "channels": ["slack"],
            "daily_buckets": [1, 0, 2, 0, 0],
        },
        "telegram:1260193629": {
            "last_ts": "2026-06-08T09:00:00Z",
            "turns_7d": 1, "turns_30d": 1, "cost_30d": 0.1,
            "sessions_30d": 1, "channels": ["telegram"],
            "daily_buckets": [1, 0, 0, 0, 0],
        },
    }
    out = ua.rewrite_slack_dm_keys(activity, {})
    assert "slack:D0AMYBZ4RM1" not in out
    assert "slack:U0REAL_USER" in out
    assert out["slack:U0REAL_USER"]["turns_30d"] == 5
    assert "telegram:1260193629" in out


def test_rewrite_slack_dm_keys_merges_when_both_paths_present(monkeypatch):
    """A user can show up via TWO paths (D-channel rewritten to U
    + senderRegistry-enriched user_id). Same person; merge them."""
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: "TOK")
    monkeypatch.setattr(
        name_resolver, "slack_im_channel_to_user",
        lambda token, channel_id: "U0MERGED")
    activity = {
        "slack:D0AMYBZ4RM1": {
            "last_ts": "2026-06-05T08:00:00Z",
            "turns_7d": 1, "turns_30d": 6,
            "cost_30d": 0.2, "sessions_30d": 1,
            "channels": ["slack"], "daily_buckets": [1, 1, 1, 1, 1, 1],
        },
        "slack:U0MERGED": {
            "last_ts": "2026-06-08T11:00:00Z",
            "turns_7d": 3, "turns_30d": 19,
            "cost_30d": 0.8, "sessions_30d": 3,
            "channels": ["slack"], "daily_buckets": [5, 5, 5, 2, 1, 1],
        },
    }
    out = ua.rewrite_slack_dm_keys(activity, {})
    assert list(out.keys()) == ["slack:U0MERGED"]
    merged = out["slack:U0MERGED"]
    assert merged["turns_30d"] == 25
    assert merged["last_ts"] == "2026-06-08T11:00:00Z"
    assert merged["daily_buckets"] == [6, 6, 6, 3, 2, 2]


def test_rewrite_slack_dm_keys_preserves_d_when_lookup_fails(monkeypatch):
    """No token → D-id key preserved (surface activity under a useless
    key beats losing the data silently)."""
    from evolve_admin.evo import name_resolver
    monkeypatch.setattr(
        name_resolver, "_channel_token", lambda net, ch, **kw: None)
    activity = {
        "slack:D0AMYBZ4RM1": {
            "last_ts": "2026-06-08T08:00:00Z",
            "turns_7d": 1, "turns_30d": 1, "cost_30d": 0.1,
            "sessions_30d": 1, "channels": ["slack"],
            "daily_buckets": [1, 0, 0],
        },
    }
    out = ua.rewrite_slack_dm_keys(activity, {})
    assert "slack:D0AMYBZ4RM1" in out
