"""tests/test_disk_reclaim.py — reclaimable-space scanner + disk_low enrichment.

PR1 of the pod-host disk-hygiene plan (docs/spec-delta-disk-reclaim-2026-06-21.md):
the report half. Covers disk_reclaim.scan_reclaimable sizing/filtering/tri-state
and the host_health disk_low Signal firing at warn with a reclaimable breakdown
and a fill projection. SCAN ONLY — nothing here deletes or truncates.

Fixture logs are sparse files (``truncate`` to a logical size) so the cap-sized
fixtures cost no real disk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import disk_reclaim, host_health  # noqa: E402

MB = 1024 * 1024
EVOLVE_CAP = disk_reclaim.EVOLVE_LOG_CAP
OC_CAP = disk_reclaim.OC_LOG_CAP


def _sparse(path: Path, size: int) -> None:
    """Create a sparse file whose st_size == ``size`` (no real blocks used)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def _fixture_tree(base: Path) -> Path:
    """A /Users-shaped tree with reclaimable and non-reclaimable content."""
    users = base / "Users"
    home = users / "alice"
    # npm cache — whole-dir, regenerable. 7 MB cacache + 1 MB npx = 8 MB.
    _sparse(home / ".npm" / "_cacache" / "index" / "a", 4 * MB)
    _sparse(home / ".npm" / "_cacache" / "b", 3 * MB)
    _sparse(home / ".npm" / "_npx" / "x", 1 * MB)
    # Evolve logs — only the oversized one counts.
    _sparse(home / ".evolve" / "logs" / "evolve.log", EVOLVE_CAP + MB)  # over → in
    _sparse(home / ".evolve" / "logs" / "small.log", 1 * MB)            # under → out
    # OpenClaw logs — only oversized ROTATED siblings count.
    _sparse(home / ".openclaw" / "logs" / "openclaw.1.log", OC_CAP + MB)  # rotated, over → in
    _sparse(home / ".openclaw" / "logs" / "openclaw.2.log", 1 * MB)       # rotated, under → out
    _sparse(home / ".openclaw" / "logs" / "openclaw.log", 10 * MB)        # live → out
    _sparse(home / ".openclaw" / "logs" / "openclaw.4.log", 8 * MB)       # suffix .4 → out
    return users


def _by_cat(result: dict) -> dict[str, dict]:
    return {c["category"]: c for c in result["categories"]}


# ── scan_reclaimable ──────────────────────────────────────────────────────────


class TestScanReclaimable:
    def test_sizes_methods_and_filtering(self, tmp_path):
        users = _fixture_tree(tmp_path)
        res = disk_reclaim.scan_reclaimable(roots=(str(users),))
        cats = _by_cat(res)

        # npm: whole-dir sizes, rm method, both subdirs as entries.
        assert cats["npm_cache"]["method"] == "rm"
        assert cats["npm_cache"]["bytes"] == 8 * MB
        assert len(cats["npm_cache"]["entries"]) == 2

        # evolve logs: only the oversized one, truncate method.
        assert cats["evolve_logs_oversized"]["method"] == "truncate"
        assert cats["evolve_logs_oversized"]["bytes"] == EVOLVE_CAP + MB
        assert len(cats["evolve_logs_oversized"]["entries"]) == 1

        # oc rotated: only openclaw.1.log (rotated + over cap).
        assert cats["oc_rotated_logs"]["method"] == "truncate"
        assert cats["oc_rotated_logs"]["bytes"] == OC_CAP + MB
        assert len(cats["oc_rotated_logs"]["entries"]) == 1
        assert cats["oc_rotated_logs"]["entries"][0]["path"].endswith("openclaw.1.log")

        # Nothing was unreadable → no category is partial.
        assert all(c["partial"] is False for c in res["categories"])
        assert res["total_bytes"] == 8 * MB + (EVOLVE_CAP + MB) + (OC_CAP + MB)

    def test_log_cap_is_inclusive(self, tmp_path):
        users = tmp_path / "Users"
        _sparse(users / "u" / ".evolve" / "logs" / "exact.log", EVOLVE_CAP)
        _sparse(users / "u" / ".evolve" / "logs" / "under.log", EVOLVE_CAP - 1)
        cats = _by_cat(disk_reclaim.scan_reclaimable(roots=(str(users),)))
        assert cats["evolve_logs_oversized"]["bytes"] == EVOLVE_CAP
        assert len(cats["evolve_logs_oversized"]["entries"]) == 1

    def test_empty_tree_is_clean_zero_not_partial(self, tmp_path):
        users = tmp_path / "Users"
        users.mkdir()
        res = disk_reclaim.scan_reclaimable(roots=(str(users),))
        # Readable + empty → 0 bytes, explicitly NOT partial.
        assert res["total_bytes"] == 0
        assert all(c["partial"] is False for c in res["categories"])

    def test_unreadable_dir_marks_partial_not_zero(self, tmp_path):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("chmod 000 does not block root")
        users = tmp_path / "Users"
        cacache = users / "bob" / ".npm" / "_cacache"
        _sparse(cacache / "a.bin", 3 * MB)
        os.chmod(cacache, 0o000)
        try:
            res = disk_reclaim.scan_reclaimable(roots=(str(users),))
        finally:
            os.chmod(cacache, 0o755)  # restore so pytest can clean up
        npm = _by_cat(res)["npm_cache"]
        # The 3 MB was unreadable: we must report partial, never a silent 0.
        assert npm["partial"] is True

    def test_does_not_follow_symlinks(self, tmp_path):
        users = tmp_path / "Users"
        outside = tmp_path / "outside"
        _sparse(outside / "huge", 9 * MB)
        cacache = users / "u" / ".npm" / "_cacache"
        cacache.mkdir(parents=True)
        _sparse(cacache / "real", 2 * MB)
        os.symlink(outside, cacache / "link")  # symlinked dir must be ignored
        npm = _by_cat(disk_reclaim.scan_reclaimable(roots=(str(users),)))["npm_cache"]
        assert npm["bytes"] == 2 * MB

    def test_trim_drops_entries_keeps_contract(self, tmp_path):
        users = _fixture_tree(tmp_path)
        res = disk_reclaim.scan_reclaimable(roots=(str(users),))
        trimmed = disk_reclaim.trim_categories_for_signal(res["categories"])
        for c in trimmed:
            assert set(c.keys()) <= {"category", "label", "bytes", "method", "partial"}
            assert "entries" not in c


# ── host_health disk_low enrichment ───────────────────────────────────────────


def _disk_snapshot(*, status="warn", percent=88.0, used=880, total=1000,
                   captured_at=0.0, hostname="mini.test") -> dict:
    return {
        "ok": True,
        "available": True,
        "hostname": hostname,
        "cpu_percent": 10.0,
        "cpu_status": "ok",
        "memory": {"percent": 10.0, "status": "ok",
                   "total_bytes": 0, "used_bytes": 0, "available_bytes": 0},
        "disk": {"percent": percent, "status": status,
                 "total_bytes": total, "used_bytes": used,
                 "free_bytes": total - used, "mount": "/Users"},
        "load_avg": {"per_cpu_1m": 0.1, "status": "ok",
                     "load1": 0, "load5": 0, "load15": 0},
        "captured_at": captured_at,
    }


def _firing(shared: Path) -> list[dict]:
    return [json.loads(f.read_text())
            for f in (shared / "signals" / "firing").glob("*.json")]


class TestDiskSignalEnrichment:
    def test_warn_fires_with_reclaimable_detail(self, tmp_path):
        users = _fixture_tree(tmp_path)
        shared = tmp_path / "shared"
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="warn"), shared, reclaim_roots=(str(users),)
        )
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        assert sig["severity"] == "warn"
        d = sig["details"]
        assert d["reclaimable_bytes"] == 8 * MB + (EVOLVE_CAP + MB) + (OC_CAP + MB)
        cats = {c["category"]: c for c in d["reclaimable"]}
        assert cats["npm_cache"]["method"] == "rm"
        assert "entries" not in cats["npm_cache"]  # trimmed for the store
        assert "reclaimable" in sig["body"]
        assert "what_it_means" in d and "fix_steps" in d

    def test_crit_escalates_to_alert(self, tmp_path):
        users = _fixture_tree(tmp_path)
        shared = tmp_path / "shared"
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="crit", percent=96.0), shared,
            reclaim_roots=(str(users),),
        )
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        assert sig["severity"] == "alert"

    def test_partial_scan_flagged_in_details(self, tmp_path):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("chmod 000 does not block root")
        users = tmp_path / "Users"
        cacache = users / "bob" / ".npm" / "_cacache"
        _sparse(cacache / "a.bin", 3 * MB)
        os.chmod(cacache, 0o000)
        shared = tmp_path / "shared"
        try:
            host_health.emit_signals_from_snapshot(
                _disk_snapshot(status="warn"), shared, reclaim_roots=(str(users),)
            )
        finally:
            os.chmod(cacache, 0o755)
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        assert sig["details"].get("reclaimable_partial") is True


# ── fill projection ───────────────────────────────────────────────────────────


class TestFillProjection:
    def test_insufficient_history_no_projection(self, tmp_path):
        users = tmp_path / "Users"  # absent → empty scan, fast
        shared = tmp_path / "shared"
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="warn", used=800, total=1000, captured_at=1000.0),
            shared, reclaim_roots=(str(users),),
        )
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        assert "fill_projection" not in sig["details"]

    def test_two_samples_over_a_day_yield_slope(self, tmp_path):
        users = tmp_path / "Users"
        shared = tmp_path / "shared"
        t0 = 1_000_000.0
        # First emit: one sample, no projection yet.
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="warn", used=800, total=1000, captured_at=t0),
            shared, reclaim_roots=(str(users),),
        )
        # Second emit two days later, +20 used → +10/day.
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="warn", used=820, total=1000,
                           captured_at=t0 + 2 * 86400),
            shared, reclaim_roots=(str(users),),
        )
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        proj = sig["details"]["fill_projection"]
        # 10/day over 1000 → 7%/week; 180 free / 10 per day → 18 days.
        assert proj["pct_per_week"] == pytest.approx(7.0, abs=0.1)
        assert proj["eta_days"] == pytest.approx(18.0, abs=0.5)
        assert proj["samples"] == 2
        assert "%/week" in sig["body"]

    def test_draining_disk_yields_no_projection(self, tmp_path):
        users = tmp_path / "Users"
        shared = tmp_path / "shared"
        t0 = 2_000_000.0
        host_health.emit_signals_from_snapshot(
            _disk_snapshot(status="warn", used=900, total=1000, captured_at=t0),
            shared, reclaim_roots=(str(users),),
        )
        host_health.emit_signals_from_snapshot(  # used DROPS
            _disk_snapshot(status="warn", used=880, total=1000,
                           captured_at=t0 + 2 * 86400),
            shared, reclaim_roots=(str(users),),
        )
        [sig] = [s for s in _firing(shared) if s["type"] == "disk_low"]
        assert "fill_projection" not in sig["details"]

    def test_history_caps_at_max_samples(self, tmp_path):
        shared = tmp_path / "shared"
        path = host_health._disk_history_path(shared)
        for i in range(host_health._DISK_HISTORY_MAX_SAMPLES + 10):
            host_health._append_disk_sample(shared, ts=float(i), used_bytes=i, total_bytes=1000)
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert len(lines) == host_health._DISK_HISTORY_MAX_SAMPLES
