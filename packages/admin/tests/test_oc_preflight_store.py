"""Tests for oc_preflight_store — the audit trail + per-pair cache for the
OC upgrade preflight (items 2 and 4 of
hold-fix-4277-preflight-fails-closed-and-is-wired-to-the-upgrade), plus the
freshness gate (hold-fix-4392-the-preflight-gate-accepts-a-report-of-any-age).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from evolve_admin import oc_preflight_store as store
from evolve_admin.oc_preflight import BotPreflightRow, PreflightReport, bot_set_hash


def _report(target="2026.9.4", blocking_row=False):
    rows = [BotPreflightRow(bot_id="team-bot-a")]
    if blocking_row:
        rows[0].workspace_outside_home = "/elsewhere"
    return PreflightReport(target_version=target, rows=rows)


# ── Audit trail (item 4) ──────────────────────────────────────────────────────


def test_persist_report_stamps_actor_timestamp_and_from_to(tmp_path):
    report = _report()
    rid = store.persist_report(
        report, actor="operator-x", installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_report(rid, shared_dir=tmp_path)
    assert data is not None
    assert data["actor"] == "operator-x"
    assert data["installed_version"] == "2026.9.2"
    assert data["target_version"] == "2026.9.4"
    assert data["checked_at"]
    assert data["report_id"] == rid


def test_persist_report_is_written_under_the_shared_dir(tmp_path):
    store.persist_report(
        _report(), actor="operator-x", installed_version="2026.9.2", shared_dir=tmp_path,
    )
    root = store.reports_dir(tmp_path)
    assert root.exists()
    assert any(p.name.endswith(".json") for p in root.iterdir())


def test_retention_sweep_keeps_only_the_most_recent_reports(tmp_path):
    # Filenames are controlled directly (rather than going through
    # persist_report's real, timestamp-prefixed ids) so the "most recent 3"
    # selection is pinned by lexical order, not by real-clock timing.
    root = store.reports_dir(tmp_path)
    root.mkdir(parents=True)
    names = [f"2020010{i}T000000Z-{i:08x}" for i in range(6)]
    for name in names:
        (root / f"{name}.json").write_text("{}")
    store._retention_sweep(root, keep=3)
    kept = {p.stem for p in root.glob("*.json")}
    assert kept == set(names[-3:])


# ── Per-pair cache (item 2) ───────────────────────────────────────────────────


def test_cache_is_keyed_by_the_exact_from_to_pair(tmp_path):
    store.persist_report(
        _report(target="2026.9.4"), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    assert store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path) is not None
    assert store.load_cached_for_pair("2026.9.2", "2026.9.5", shared_dir=tmp_path) is None
    assert store.load_cached_for_pair("2026.9.3", "2026.9.4", shared_dir=tmp_path) is None


def test_a_second_run_for_the_same_pair_overwrites_the_cache_entry(tmp_path):
    first = store.persist_report(
        _report(target="2026.9.4", blocking_row=True), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    second = store.persist_report(
        _report(target="2026.9.4", blocking_row=False), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    cached = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    assert cached["report_id"] == second
    assert cached["report_id"] != first
    assert cached["blocking"] is False


def test_missing_installed_version_still_produces_a_stable_key(tmp_path):
    # installed_version unreadable is itself one of the fail-closed shapes
    # (item 1) — the cache must not crash or collide with a real version.
    store.persist_report(
        _report(target="2026.9.4"), actor="operator-x",
        installed_version=None, shared_dir=tmp_path,
    )
    assert store.load_cached_for_pair(None, "2026.9.4", shared_dir=tmp_path) is not None
    assert store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path) is None


# ── Background runner ──────────────────────────────────────────────────────────


def test_start_background_check_persists_and_clears_inflight(tmp_path, monkeypatch):
    # Stub preflight() itself — start_background_check's own job is
    # threading + persistence, not re-proving oc_preflight.preflight()'s
    # behavior, and the real thing would attempt a live npm fetch.
    monkeypatch.setattr(store, "preflight", lambda network, target_version, **kw: _report(target=target_version))
    network = {"bots": {}}
    rid, status = store.start_background_check(
        network, "2026.9.4", actor="operator-x", installed_version="2026.9.2",
        shared_dir=tmp_path,
    )
    assert status == "running"
    for _ in range(50):
        if store.inflight_report_id() is None:
            break
        time.sleep(0.02)
    assert store.inflight_report_id() is None
    data = store.load_report(rid, shared_dir=tmp_path)
    assert data is not None
    assert data["actor"] == "operator-x"


def test_a_second_check_while_one_is_running_returns_the_inflight_id(tmp_path, monkeypatch):
    import threading

    release = threading.Event()

    def _slow_preflight(network, target_version, **kw):
        release.wait(timeout=5)
        return _report(target=target_version)

    monkeypatch.setattr(store, "preflight", _slow_preflight)
    network = {"bots": {}}
    rid1, status1 = store.start_background_check(
        network, "2026.9.4", actor="operator-x", installed_version="2026.9.2",
        shared_dir=tmp_path,
    )
    rid2, status2 = store.start_background_check(
        network, "2026.9.4", actor="operator-x", installed_version="2026.9.2",
        shared_dir=tmp_path,
    )
    release.set()
    assert status1 == "running"
    assert status2 == "running"
    assert rid1 == rid2


# ── Freshness gate (hold-fix-4392) ─────────────────────────────────────────────

_NETWORK = {"bots": {"team-bot-a": {}}}
_HASH = bot_set_hash(_NETWORK)


def _fresh_report(target_version="2026.9.4", **kw):
    rows = [BotPreflightRow(bot_id="team-bot-a")]
    return PreflightReport(
        target_version=target_version, rows=rows, bot_set_hash=_HASH, **kw,
    )


def test_fresh_and_matching_hash_is_accepted(tmp_path):
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    assert store.pair_freshness_reason(data, bot_set_hash=_HASH) is None


def test_a_report_older_than_the_window_is_stale(tmp_path):
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert store.pair_freshness_reason(data, now=later, bot_set_hash=_HASH) == "stale"


def test_a_report_just_under_the_window_is_still_fresh(tmp_path):
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    later = datetime.now(timezone.utc) + timedelta(hours=23)
    assert store.pair_freshness_reason(data, now=later, bot_set_hash=_HASH) is None


def test_a_bot_added_since_the_report_ran_is_bot_set_changed(tmp_path):
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    live_hash = bot_set_hash({"bots": {"team-bot-a": {}, "team-bot-b": {}}})
    assert store.pair_freshness_reason(data, bot_set_hash=live_hash) == "bot_set_changed"


def test_unparsable_checked_at_is_stale_not_a_crash(tmp_path):
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    data["checked_at"] = "not-a-timestamp"
    assert store.pair_freshness_reason(data, bot_set_hash=_HASH) == "stale"


def test_no_bot_set_hash_given_skips_that_check(tmp_path):
    # A caller that doesn't state the current registry (or a fixture written
    # before this field existed) gets the age check only, never a crash on
    # a None-vs-None comparison it didn't ask for.
    store.persist_report(
        _fresh_report(), actor="operator-x",
        installed_version="2026.9.2", shared_dir=tmp_path,
    )
    data = store.load_cached_for_pair("2026.9.2", "2026.9.4", shared_dir=tmp_path)
    assert store.pair_freshness_reason(data) is None


def test_missing_entry_reason_is_missing(tmp_path):
    assert store.pair_freshness_reason(None) == "missing"


def test_latest_cache_for_installed_picks_the_most_recently_checked(tmp_path):
    # Pair-cache files written directly (rather than through persist_report,
    # whose checked_at is real-clock and second-resolution) so "most recent"
    # is pinned by the fixture, not by test timing.
    root = store.reports_dir(tmp_path)
    root.mkdir(parents=True)
    for target, checked_at in (
        ("2026.9.4", "2026-09-01T00:00:00Z"), ("2026.9.5", "2026-09-02T00:00:00Z"),
    ):
        (root / f"pair-2026.9.2__{target}.json").write_text(
            '{"installed_version": "2026.9.2", "target_version": "%s", '
            '"checked_at": "%s"}' % (target, checked_at)
        )
    latest = store.latest_cache_for_installed("2026.9.2", shared_dir=tmp_path)
    assert latest is not None
    assert latest["target_version"] == "2026.9.5"
