"""tests/test_investigation_toolkit.py — investigation toolkit unit tests.

Each tool is exercised with hand-rolled fixtures + injected readers so
the production import path (cost_ledger / signals / evolve_config /
evolve_admin) isn't required.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from investigation.toolkit import (  # noqa: E402
    ConfigIntent,
    CorrelatedSignal,
    FileSize,
    config_intent,
    correlated_signals,
    file_top_contributors,
    recent_config_changes,
    time_series_cost_per_call,
)


# ── correlated_signals ───────────────────────────────────────────────────────


class _FakeSignal:
    """Stand-in for schema.signal.Signal — only the fields the toolkit reads."""

    def __init__(
        self,
        *,
        id: str,
        bot_id: str,
        producer: str,
        type: str,
        severity: str = "warn",
        title: str = "",
        signature: str = "",
        details: dict | None = None,
    ):
        self.id = id
        self.bot_id = bot_id
        self.producer = producer
        self.type = type
        self.severity = severity
        self.title = title
        self.signature = signature
        self.details = details or {}


def test_correlated_signals_returns_active_on_same_bot(monkeypatch, tmp_path):
    """All firing/snoozed signals on the bot pass through, except excluded."""
    fake_signals = [
        _FakeSignal(
            id="s1", bot_id="security_bot", producer="cost_watchdog",
            type="workspace_growth", signature="security_bot/memory.md",
        ),
        _FakeSignal(
            id="s2", bot_id="security_bot", producer="cost_watchdog",
            type="cache_envelope_growth", signature="security_bot",
        ),
        # Should be excluded by signature
        _FakeSignal(
            id="s3", bot_id="security_bot", producer="cost_watchdog",
            type="efficiency_drift", signature="security_bot/low",
        ),
    ]
    import signals.store as store
    monkeypatch.setattr(
        store, "iter_active",
        lambda shared_dir, **kw: iter(
            s for s in fake_signals if kw.get("bot_id") in (None, s.bot_id)
        ),
    )

    out = correlated_signals(
        tmp_path, "security_bot", exclude_signatures={"security_bot/low"}
    )
    types = sorted(c.type for c in out)
    assert types == ["cache_envelope_growth", "workspace_growth"]


def test_correlated_signals_filters_by_type(monkeypatch, tmp_path):
    fake_signals = [
        _FakeSignal(id="s1", bot_id="security_bot", producer="cost_watchdog",
                    type="workspace_growth", signature="a"),
        _FakeSignal(id="s2", bot_id="security_bot", producer="cost_watchdog",
                    type="something_else", signature="b"),
    ]
    import signals.store as store
    monkeypatch.setattr(store, "iter_active",
                        lambda shared_dir, **kw: iter(fake_signals))
    out = correlated_signals(
        tmp_path, "security_bot", types=["workspace_growth"]
    )
    assert [c.type for c in out] == ["workspace_growth"]


def test_correlated_signals_failopen_on_missing_module(monkeypatch, tmp_path):
    """If signals.store raises during iter_active, return empty rather than crash."""
    import signals.store as store

    def bad_iter(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(store, "iter_active", bad_iter)
    assert correlated_signals(tmp_path, "security_bot") == []


# ── recent_config_changes ────────────────────────────────────────────────────


def test_recent_config_changes_filters_window(tmp_path):
    bot_home = tmp_path / "security_bot"
    (bot_home / ".openclaw").mkdir(parents=True)
    recent = bot_home / ".openclaw" / "openclaw.json"
    old = bot_home / ".openclaw" / "auth-profiles.json"
    recent.write_text("{}")
    old.write_text("{}")

    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    # Backdate `old` to 10 days ago
    import os
    old_ts = (now - timedelta(days=10)).timestamp()
    os.utime(old, (old_ts, old_ts))

    out = recent_config_changes(
        "security_bot",
        [".openclaw/openclaw.json", ".openclaw/auth-profiles.json", "missing.json"],
        since_days=7,
        bot_home=bot_home,
        now=now,
    )
    paths = sorted(p for p, _ in out)
    assert paths == [".openclaw/openclaw.json"]


def test_recent_config_changes_empty_on_missing_bot_home(tmp_path):
    out = recent_config_changes(
        "personal_bot",
        [".openclaw/openclaw.json"],
        since_days=7,
        bot_home=tmp_path / "does-not-exist",
    )
    assert out == []


# ── config_intent ────────────────────────────────────────────────────────────


def test_config_intent_returns_typed_record(monkeypatch):
    from evolve_admin import config_intent as ci
    monkeypatch.setattr(
        ci, "get_intent",
        lambda bot_id, field_path, **kw: {
            "id": "intent-abc",
            "set_at": "2026-05-01T00:00:00Z",
            "reason": "operator deliberately set this",
        },
    )
    out = config_intent("security_bot", "tools.exec.security")
    assert isinstance(out, ConfigIntent)
    assert out.intent_id == "intent-abc"
    assert out.reason.startswith("operator")


def test_config_intent_returns_none_when_absent(monkeypatch):
    from evolve_admin import config_intent as ci
    monkeypatch.setattr(ci, "get_intent",
                        lambda bot_id, field_path, **kw: None)
    assert config_intent("security_bot", "some.field") is None


def test_config_intent_failopen_on_raise(monkeypatch):
    from evolve_admin import config_intent as ci

    def bad(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(ci, "get_intent", bad)
    assert config_intent("security_bot", "field") is None


# ── file_top_contributors ────────────────────────────────────────────────────


def test_file_top_contributors_sorts_descending():
    sizes = {
        "memory/2026-05-02.md": 196_000,
        "HEARTBEAT.md": 5_000,
        "memory/2026-05-03.md": 100_000,
        "memory/journal/2026-05-15.md": 5_000,
    }
    out = file_top_contributors("security_bot", n=2, sizes=sizes)
    assert [f.path for f in out] == [
        "memory/2026-05-02.md",
        "memory/2026-05-03.md",
    ]
    assert out[0].size_bytes == 196_000


def test_file_top_contributors_handles_empty():
    assert file_top_contributors("personal_bot", sizes={}) == []


# ── manifest_mentions ────────────────────────────────────────────────────────


def _write_manifest(manifests_dir: Path, name: str, payload: dict) -> None:
    import json
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / name).write_text(json.dumps(payload))


def test_manifest_mentions_finds_match_in_searchable_fields(tmp_path):
    """Tool name appearing in display_name / files / purpose etc. → match.

    Bot tries to exec python3; manifest declares an app that runs
    python scripts → mechanically-wrong-state evidence.
    """
    from investigation.toolkit import manifest_mentions
    _write_manifest(tmp_path, "i-15709081.json", {
        "instance_id": "i-15709081",
        "display_name": "Protein Tracker",
        "purpose": "Run python3 scripts to ingest macro data",
        "files": ["scripts/ingest_protein.py"],
    })
    out = manifest_mentions("team_bot_a", "python3", manifests_dir=tmp_path)
    assert len(out) == 1
    assert out[0].app_id == "i-15709081"
    assert out[0].app_name == "Protein Tracker"
    assert out[0].manifest_path == "i-15709081.json"
    assert "python3" in out[0].snippet.lower()


def test_manifest_mentions_case_insensitive(tmp_path):
    from investigation.toolkit import manifest_mentions
    _write_manifest(tmp_path, "i-abc.json", {
        "display_name": "Curl wrapper",
        "purpose": "Call CURL to fetch APIs",
    })
    out = manifest_mentions("team_bot_a", "curl", manifests_dir=tmp_path)
    assert len(out) == 1


def test_manifest_mentions_no_match_returns_empty(tmp_path):
    from investigation.toolkit import manifest_mentions
    _write_manifest(tmp_path, "i-abc.json", {
        "display_name": "Backup",
        "purpose": "Snapshot and rotate workspace",
    })
    out = manifest_mentions("team_bot_a", "python3", manifests_dir=tmp_path)
    assert out == []


def test_manifest_mentions_skips_dotfiles_and_non_json(tmp_path):
    from investigation.toolkit import manifest_mentions
    _write_manifest(tmp_path, ".scan-status.json", {
        "purpose": "tracks scanner state mentioning python3",
    })
    (tmp_path / "README.md").write_text("Bot uses python3")
    out = manifest_mentions("team_bot_a", "python3", manifests_dir=tmp_path)
    assert out == []


def test_manifest_mentions_skips_bookkeeping_fields(tmp_path):
    """Match against `display_name` / `purpose` / `files` etc.; skip
    timestamps + audit-bookkeeping fields that would give noise hits."""
    from investigation.toolkit import manifest_mentions
    _write_manifest(tmp_path, "i-1.json", {
        # Only bookkeeping fields mention "python3" — should NOT match
        "created_at": "2026-05-28T12:00:00Z by python3",
        "last_test_output": "subprocess: /usr/bin/python3 ran",
        # display_name + purpose do NOT mention python3
        "display_name": "Notifier",
        "purpose": "Send notifications to operator",
    })
    out = manifest_mentions("team_bot_a", "python3", manifests_dir=tmp_path)
    assert out == []


def test_manifest_mentions_caps_at_max_mentions(tmp_path):
    from investigation.toolkit import manifest_mentions
    for i in range(10):
        _write_manifest(tmp_path, f"i-{i:02d}.json", {
            "display_name": f"App {i}",
            "purpose": "Run python3 for stuff",
        })
    out = manifest_mentions(
        "team_bot_a", "python3", manifests_dir=tmp_path, max_mentions=3,
    )
    assert len(out) == 3


def test_manifest_mentions_missing_dir_returns_empty(tmp_path):
    from investigation.toolkit import manifest_mentions
    out = manifest_mentions(
        "team_bot_a", "python3",
        manifests_dir=tmp_path / "does-not-exist",
    )
    assert out == []


def test_manifest_mentions_skips_invalid_json(tmp_path):
    from investigation.toolkit import manifest_mentions
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "broken.json").write_text("not valid json {{{")
    _write_manifest(tmp_path, "i-good.json", {
        "display_name": "Good App",
        "purpose": "Run python3 for stuff",
    })
    out = manifest_mentions("team_bot_a", "python3", manifests_dir=tmp_path)
    # The broken manifest is skipped silently; the good one matches.
    assert len(out) == 1
    assert out[0].app_name == "Good App"


# ── time_series_cost_per_call ────────────────────────────────────────────────


def _evt(*, ts: str, model: str, cost: float) -> dict:
    return {
        "ts": ts, "model": model, "cost_usd": cost, "bot_id": "security_bot",
    }


def test_time_series_cost_per_call_aggregates_by_day(tmp_path):
    events = [
        _evt(ts="2026-05-26T10:00:00Z", model="claude-haiku-4-5", cost=0.01),
        _evt(ts="2026-05-26T11:00:00Z", model="claude-haiku-4-5", cost=0.03),
        _evt(ts="2026-05-27T10:00:00Z", model="claude-haiku-4-5", cost=0.05),
    ]

    def reader(bot_id, days, shared_dir, *, now):
        return iter(events)

    out = time_series_cost_per_call(
        tmp_path, "security_bot", days=7, events_reader=reader,
        now=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )
    # 26th: (0.01 + 0.03) / 2 = 0.02 ; 27th: 0.05 / 1 = 0.05
    assert out == [
        ("2026-05-26", 0.02, 2),
        ("2026-05-27", 0.05, 1),
    ]


def test_time_series_cost_per_call_filters_by_tier(tmp_path):
    events = [
        _evt(ts="2026-05-26T10:00:00Z", model="claude-haiku-4-5", cost=0.01),
        _evt(ts="2026-05-26T11:00:00Z", model="claude-sonnet-4-6", cost=0.03),
    ]

    def reader(bot_id, days, shared_dir, *, now):
        return iter(events)

    out_low = time_series_cost_per_call(
        tmp_path, "security_bot", days=7, model_tier="low",
        events_reader=reader, now=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )
    assert out_low == [("2026-05-26", 0.01, 1)]

    out_high = time_series_cost_per_call(
        tmp_path, "security_bot", days=7, model_tier="high",
        events_reader=reader, now=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )
    assert out_high == [("2026-05-26", 0.03, 1)]


def test_time_series_cost_per_call_failopen_on_reader_raise(tmp_path):
    def reader(*a, **kw):
        raise RuntimeError("simulated")

    out = time_series_cost_per_call(
        tmp_path, "security_bot", days=7, events_reader=reader,
        now=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )
    assert out == []
