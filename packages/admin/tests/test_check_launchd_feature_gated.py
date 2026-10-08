"""End-to-end test for the feature-gated-plist branch of ``_check_launchd``.

The pre-fix bug (caught 2026-05-29): standard-profile installs reported
"Plist not found: /Library/LaunchDaemons/ai.evolve.evolve.upstream-issues-watcher.plist"
on every Pod Health scan even though the plist's absence was intentional —
``_maybe_install_launchd_upstream_issues_watcher`` skips install when the
``upstream_issues_watcher`` gate is off, but the label is still in
``expected_plist_labels`` so the orphan-sweeper doesn't delete it when the
feature flips off mid-uptime.

These tests pin the fix: a gated-off label should NOT produce a FAIL when its
plist is absent; a regular (non-gated) label still does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import health  # noqa: E402


def _seed_pod(tmp_path: Path, *, profile: str = "standard") -> tuple[Path, Path, dict]:
    """Create a tmp shared dir with install.json + a tmp /Library/LaunchDaemons
    surrogate. Returns (shared_dir, launchd_dir, network)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "install.json").write_text(json.dumps({"feature_profile": profile}))
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    network = {"sharedDir": str(shared), "members": []}
    return shared, launchd, network


def _write_plist(launchd: Path, label: str) -> None:
    """Write a minimal well-formed plist so ``_validate_plist_content`` passes."""
    (launchd / f"{label}.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'<key>Label</key><string>{label}</string>\n'
        '<key>ProgramArguments</key><array><string>/bin/true</string></array>\n'
        '</dict></plist>\n'
    )


def _no_fail_for(report: health.HealthReport, label: str) -> bool:
    """True iff there is no FAIL CheckResult whose name mentions ``label``."""
    return not any(
        c.status == health.FAIL and label in c.name
        for c in report.checks
    )


def test_gated_off_missing_plist_does_not_fail(tmp_path):
    """Standard-profile install + upstream-issues-watcher missing → no FAIL.

    The pre-fix bug surfaced exactly this case as a hard FAIL on every scan.
    """
    shared, launchd, network = _seed_pod(tmp_path, profile="standard")
    # Only the gated label is in the expected set; it's deliberately missing
    # from /Library/LaunchDaemons/.
    expected = {"ai.evolve.evolve.upstream-issues-watcher"}

    report = health.HealthReport()
    with patch.object(health, "LAUNCHD_DIR", launchd), \
         patch.object(health, "expected_plist_labels", return_value=expected), \
         patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    assert _no_fail_for(report, "ai.evolve.evolve.upstream-issues-watcher"), (
        "feature-off gated label should not produce a FAIL; checks="
        f"{[(c.status, c.name, c.detail) for c in report.checks]}"
    )

    # The summary should note the gated-off skip and not pretend the install
    # is broken ("0/0 expected" is the healthy shape on this synthetic pod).
    summary = next(c for c in report.checks if c.name in ("launchd:all", "launchd:installed"))
    assert summary.status == health.PASS
    assert "feature-gated, off" in summary.detail


def test_gated_on_missing_plist_still_fails(tmp_path):
    """Developer-profile install + watcher plist missing → FAIL stays.

    Symmetric guard: the gate skip must not mask real breakage when the
    operator actually opted into the feature.
    """
    shared, launchd, network = _seed_pod(tmp_path, profile="developer")
    expected = {"ai.evolve.evolve.upstream-issues-watcher"}

    report = health.HealthReport()
    with patch.object(health, "LAUNCHD_DIR", launchd), \
         patch.object(health, "expected_plist_labels", return_value=expected), \
         patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    fails = [
        c for c in report.checks
        if c.status == health.FAIL
        and "ai.evolve.evolve.upstream-issues-watcher" in c.name
    ]
    assert len(fails) == 1, (
        "gated-feature-on must still FAIL on missing plist; checks="
        f"{[(c.status, c.name, c.detail) for c in report.checks]}"
    )
    assert "Plist not found" in fails[0].detail


def test_non_gated_missing_plist_still_fails(tmp_path):
    """Non-gated labels (e.g. heal) keep the existing FAIL behaviour."""
    shared, launchd, network = _seed_pod(tmp_path, profile="standard")
    expected = {"ai.evolve.evolve.heal"}

    report = health.HealthReport()
    with patch.object(health, "LAUNCHD_DIR", launchd), \
         patch.object(health, "expected_plist_labels", return_value=expected), \
         patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    fails = [
        c for c in report.checks
        if c.status == health.FAIL and "ai.evolve.evolve.heal" in c.name
    ]
    assert len(fails) == 1


def test_summary_denominator_excludes_gated_off(tmp_path):
    """``X/Y plists present`` should not count gated-off labels in Y —
    otherwise operators see ``8/9`` on a perfectly healthy standard-profile
    install and chase the missing one."""
    shared, launchd, network = _seed_pod(tmp_path, profile="standard")
    # One real label installed, one gated-off label intentionally absent.
    _write_plist(launchd, "ai.evolve.evolve.heal")
    expected = {
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.upstream-issues-watcher",
    }

    report = health.HealthReport()
    with patch.object(health, "LAUNCHD_DIR", launchd), \
         patch.object(health, "expected_plist_labels", return_value=expected), \
         patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    summary = next(c for c in report.checks if c.name == "launchd:all")
    assert summary.status == health.PASS
    # 1 applicable label, not 2.
    assert "All 1 expected plists" in summary.detail
    assert "1 feature-gated, off" in summary.detail
