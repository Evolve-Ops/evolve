"""Tests for the one-time _ingested backlog purge (META:footprint).

Covers the auditor-grade guard (only literal ``_ingested`` dirs or
``<YYYY-MM-DD>`` date-dirs directly under one are removable; everything else
is refused), the dry-run (deletes nothing), and the age-filtered window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from evolve_admin import purge_ingested as pi


def _mk_ingested(root: Path, day: str, n_files: int = 3) -> Path:
    """Create ``root/_ingested/<day>/`` with *n_files* dummy records."""
    day_dir = root / "_ingested" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (day_dir / f"rec-{i}.json").write_text('{"x":1}')
    return day_dir


# ── Guard ────────────────────────────────────────────────────────────────────


def test_guard_accepts_ingested_root(tmp_path: Path):
    ingested = tmp_path / "audit_outbox" / "_ingested"
    ingested.mkdir(parents=True)
    pi.assert_safe_ingested_target(ingested, ingested)  # no raise


def test_guard_accepts_date_dir_under_ingested(tmp_path: Path):
    ingested = tmp_path / "_ingested"
    day = _mk_ingested(tmp_path, "2026-06-01")
    pi.assert_safe_ingested_target(day, ingested)  # no raise


def test_guard_refuses_non_ingested_dir(tmp_path: Path):
    ingested = tmp_path / "_ingested"
    ingested.mkdir()
    # A sibling that is NOT _ingested must be refused even if asked.
    live = tmp_path / "audit_outbox"
    live.mkdir()
    with pytest.raises(pi.UnsafePurgeTarget):
        pi.assert_safe_ingested_target(live, ingested)


def test_guard_refuses_non_date_child(tmp_path: Path):
    ingested = tmp_path / "_ingested"
    bad = ingested / "not-a-date"
    bad.mkdir(parents=True)
    with pytest.raises(pi.UnsafePurgeTarget):
        pi.assert_safe_ingested_target(bad, ingested)


def test_guard_refuses_grandchild(tmp_path: Path):
    ingested = tmp_path / "_ingested"
    deep = ingested / "2026-06-01" / "sub"
    deep.mkdir(parents=True)
    with pytest.raises(pi.UnsafePurgeTarget):
        pi.assert_safe_ingested_target(deep, ingested)


def test_guard_refuses_symlink(tmp_path: Path):
    ingested = tmp_path / "_ingested"
    ingested.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    link = ingested / "2026-06-01"
    link.symlink_to(real)
    with pytest.raises(pi.UnsafePurgeTarget):
        pi.assert_safe_ingested_target(link, ingested)


def test_guard_refuses_root_not_named_ingested(tmp_path: Path):
    notroot = tmp_path / "outbox"
    notroot.mkdir()
    with pytest.raises(pi.UnsafePurgeTarget):
        pi.assert_safe_ingested_target(notroot, notroot)


# ── _purge_one ───────────────────────────────────────────────────────────────


def test_purge_all_removes_whole_ingested(tmp_path: Path):
    outbox = tmp_path / "audit_outbox"
    _mk_ingested(outbox, "2026-06-01", 3)
    _mk_ingested(outbox, "2026-06-02", 2)
    ingested = outbox / "_ingested"

    res = pi._purge_one("bot1", ingested, dry_run=False, cutoff_date=None)

    assert res.files_removed == 5
    assert res.date_dirs_removed == 2
    assert res.bytes_removed > 0
    assert not res.errors
    assert not ingested.exists()
    # The live outbox root is untouched.
    assert outbox.exists()


def test_dry_run_deletes_nothing(tmp_path: Path):
    outbox = tmp_path / "audit_outbox"
    _mk_ingested(outbox, "2026-06-01", 4)
    ingested = outbox / "_ingested"

    res = pi._purge_one("bot1", ingested, dry_run=True, cutoff_date=None)

    assert res.files_removed == 4  # counted...
    assert ingested.exists()  # ...but nothing deleted
    assert (ingested / "2026-06-01" / "rec-0.json").exists()


def test_absent_ingested_is_skipped(tmp_path: Path):
    res = pi._purge_one(
        "bot1", tmp_path / "audit_outbox" / "_ingested",
        dry_run=False, cutoff_date=None,
    )
    assert res.skipped_reason == "absent"
    assert res.files_removed == 0
    assert not res.errors


def test_age_filter_keeps_recent_removes_old(tmp_path: Path):
    outbox = tmp_path / "audit_outbox"
    _mk_ingested(outbox, "2026-05-01", 3)  # old
    _mk_ingested(outbox, "2026-06-27", 2)  # recent
    _mk_ingested(outbox, "garbage", 1)     # non-date — must be left
    ingested = outbox / "_ingested"

    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    cutoff = (now.date())  # older_than_days handled by caller; emulate 30d here
    from datetime import timedelta
    cutoff = (now - timedelta(days=30)).date()

    res = pi._purge_one("bot1", ingested, dry_run=False, cutoff_date=cutoff)

    assert res.date_dirs_removed == 1
    assert res.files_removed == 3
    assert not (ingested / "2026-05-01").exists()
    assert (ingested / "2026-06-27").exists()
    assert (ingested / "garbage").exists()  # non-date untouched


# ── purge_ingested_backlog (integration via monkeypatched enumeration) ───────


def test_enumerate_includes_retired_bot_not_in_network(tmp_path: Path, monkeypatch):
    """A retired account with a leftover _ingested dir but NO network entry
    must still be enumerated — the filesystem scan is the authoritative set."""
    import dataclasses

    import platform_profile

    home_root = tmp_path / "Users"
    # Retired bot: on disk, absent from network.
    _mk_ingested(home_root / "retiredbot" / ".openclaw" / "workspace" / "evolve" / "audit_outbox", "2026-06-01", 2)
    # Active bot: on disk AND in network.
    _mk_ingested(home_root / "activebot" / ".openclaw" / "workspace" / "evolve" / "audit_outbox", "2026-06-01", 1)
    # An unrelated account with no outbox — must be ignored.
    (home_root / "adminacct").mkdir(parents=True)

    fake_profile = dataclasses.replace(
        platform_profile.MACOS, user_home_root=str(home_root)
    )
    monkeypatch.setattr(platform_profile, "get_profile", lambda *a, **k: fake_profile)

    shared = tmp_path / "shared"
    network = {"bots": {"activebot": {"user": "activebot"}}}
    targets = pi._enumerate_targets(shared, network)

    labels = {label for label, _ in targets}
    assert "retiredbot" in labels  # retired bot caught by the scan
    assert "activebot" in labels
    assert "adminacct" not in labels  # no outbox → not a target
    assert "<infra>" in labels


def test_backlog_aggregates_across_targets(tmp_path: Path, monkeypatch):
    outbox_a = tmp_path / "a" / "audit_outbox"
    outbox_b = tmp_path / "b" / "audit_outbox"
    infra = tmp_path / "shared" / "infra_audit_outbox"
    _mk_ingested(outbox_a, "2026-06-01", 3)
    _mk_ingested(outbox_b, "2026-06-01", 2)
    _mk_ingested(infra, "2026-06-01", 5)

    targets = [
        ("botA", outbox_a / "_ingested"),
        ("botB", outbox_b / "_ingested"),
        ("<infra>", infra / "_ingested"),
    ]
    monkeypatch.setattr(pi, "_enumerate_targets", lambda sd, net: targets)

    res = pi.purge_ingested_backlog(tmp_path / "shared", dry_run=False)

    assert res.total_files == 10
    assert len(res.targets) == 3
    for _, ingested in targets:
        assert not ingested.exists()
