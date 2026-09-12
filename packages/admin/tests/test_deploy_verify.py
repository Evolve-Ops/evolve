"""tests/test_deploy_verify.py — post-condition verifications for the
upgrade pipeline.

The motivating bug class: ``evolve-admin upgrade`` reports "✓ Plugin
rebuilt" / "✓ team_bot_a deployed" without verifying that the install dir
actually got the new build, that the gateway actually restarted, etc.
This test file covers each verifier in isolation against fake
filesystems / fake subprocess outputs so the deploy pipeline can call
them with confidence.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# verify_plugin_install_matches_source
# ─────────────────────────────────────────────────────────────────────────────


def _make_plugin_dist(root: Path, *, index_content: str, observer_content: str) -> Path:
    """Create a fake plugin dist tree with the two canonical files
    the verifier checks. Returns the dist root."""
    (root / "observer").mkdir(parents=True, exist_ok=True)
    (root / "index.js").write_text(index_content)
    (root / "observer" / "TurnObserver.js").write_text(observer_content)
    return root


def test_plugin_install_matches_when_source_and_install_identical(tmp_path):
    """Happy path — both dist trees have identical content for the
    canonical files. Returns ok=True with a confident summary."""
    from evolve_admin.deploy_verify import verify_plugin_install_matches_source

    src = _make_plugin_dist(
        tmp_path / "src" / "dist",
        index_content="export const VERSION='1';",
        observer_content="// turn observer body",
    )
    install = _make_plugin_dist(
        tmp_path / "install" / "dist",
        index_content="export const VERSION='1';",
        observer_content="// turn observer body",
    )
    r = verify_plugin_install_matches_source(src, install)
    assert r.ok is True
    assert "matches" in r.summary.lower()


def test_plugin_install_does_not_match_when_one_file_differs(tmp_path):
    """The actual production bug: tsc rebuilt source dist but cp to
    install never ran (or partial). Detect by md5 mismatch."""
    from evolve_admin.deploy_verify import verify_plugin_install_matches_source

    src = _make_plugin_dist(
        tmp_path / "src" / "dist",
        index_content="export const VERSION='2';",  # new
        observer_content="// new observer body",     # new
    )
    install = _make_plugin_dist(
        tmp_path / "install" / "dist",
        index_content="export const VERSION='2';",  # matches
        observer_content="// OLD observer body",     # mismatch
    )
    r = verify_plugin_install_matches_source(src, install)
    assert r.ok is False
    assert "TurnObserver.js" in r.detail
    assert "src=" in r.detail and "install=" in r.detail


def test_plugin_install_missing_file_is_failure(tmp_path):
    """Install dir is empty / partial — verifier flags missing files,
    not just hash mismatches."""
    from evolve_admin.deploy_verify import verify_plugin_install_matches_source

    src = _make_plugin_dist(
        tmp_path / "src" / "dist",
        index_content="x", observer_content="y",
    )
    install_dir = tmp_path / "install" / "dist"  # nothing inside it

    r = verify_plugin_install_matches_source(src, install_dir)
    assert r.ok is False
    assert "missing" in r.detail.lower()


def test_plugin_install_source_missing_is_also_failure(tmp_path):
    """Symmetric — if the source dist is empty (build failed silently)
    the verifier flags it. Catches a case where build_plugin would
    otherwise silently sync nothing-over-something."""
    from evolve_admin.deploy_verify import verify_plugin_install_matches_source

    src_dir = tmp_path / "src" / "dist"  # empty
    install = _make_plugin_dist(
        tmp_path / "install" / "dist",
        index_content="x", observer_content="y",
    )
    r = verify_plugin_install_matches_source(src_dir, install)
    assert r.ok is False
    assert "missing" in r.detail.lower()


# ─────────────────────────────────────────────────────────────────────────────
# verify_process_restarted
# ─────────────────────────────────────────────────────────────────────────────


def test_process_restart_ok_when_pid_started_after_anchor():
    """The good case — kickstart succeeded, new process is running
    with start_time > began_at."""
    from evolve_admin.deploy_verify import verify_process_restarted

    began = time.time() - 10  # upgrade started 10s ago
    r = verify_process_restarted(
        label="ai.evolve.evolve.admin-ui",
        began_at_epoch=began,
        pid_lookup=lambda label: 12345,
        start_time_lookup=lambda pid: time.time() - 5,  # started 5s ago (after began)
    )
    assert r.ok is True
    assert "12345" in r.summary
    assert "restarted" in r.summary.lower()


def test_process_restart_fails_when_pid_predates_anchor():
    """The motivating bug — kickstart silently no-op'd, daemon is
    still running the previous PID from before the upgrade. PID
    start time is BEFORE began_at."""
    from evolve_admin.deploy_verify import verify_process_restarted

    began = time.time() - 10
    r = verify_process_restarted(
        label="ai.evolve.evolve.admin-ui",
        began_at_epoch=began,
        pid_lookup=lambda label: 9999,
        start_time_lookup=lambda pid: time.time() - 3600,  # started an hour ago
    )
    assert r.ok is False
    assert "still running" in r.summary.lower() or "pre-upgrade" in r.summary.lower()
    assert "9999" in r.summary


def test_process_restart_fails_when_pid_not_found():
    """launchctl returns no PID → daemon not loaded. Different failure
    from "pre-existing PID" but same surface (ok=False, detail explains)."""
    from evolve_admin.deploy_verify import verify_process_restarted

    r = verify_process_restarted(
        label="missing-daemon",
        began_at_epoch=time.time() - 10,
        pid_lookup=lambda label: None,
    )
    assert r.ok is False
    assert "not running" in r.summary.lower() or "no pid" in r.detail.lower()


def test_process_restart_fails_when_start_time_unreadable():
    """Edge — pid exists but ps doesn't return parseable output. Fail
    closed (don't claim success when we can't verify)."""
    from evolve_admin.deploy_verify import verify_process_restarted

    r = verify_process_restarted(
        label="weird-daemon",
        began_at_epoch=time.time() - 10,
        pid_lookup=lambda label: 12345,
        start_time_lookup=lambda pid: None,
    )
    assert r.ok is False
    assert "unreadable" in r.summary.lower() or "lstart" in r.detail.lower()


# ─────────────────────────────────────────────────────────────────────────────
# verify_bot_gateway_running_new_plugin
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_gateway_verification_uses_correct_label():
    """Sanity — the bot-gateway verifier delegates to verify_process_restarted
    with the ``ai.openclaw.<bot>-gateway`` label convention."""
    from evolve_admin.deploy_verify import verify_bot_gateway_running_new_plugin

    seen_labels: list[str] = []
    def _lookup(label):
        seen_labels.append(label)
        return None

    verify_bot_gateway_running_new_plugin(
        bot_id="team_bot_a",
        deploy_began_at_epoch=time.time(),
        pid_lookup=_lookup,
    )
    assert seen_labels == ["ai.openclaw.team_bot_a-gateway"]


def test_bot_gateway_verification_passes_when_gateway_restarted():
    from evolve_admin.deploy_verify import verify_bot_gateway_running_new_plugin

    began = time.time() - 30
    r = verify_bot_gateway_running_new_plugin(
        bot_id="team_bot_a",
        deploy_began_at_epoch=began,
        pid_lookup=lambda label: 22222,
        start_time_lookup=lambda pid: began + 5,  # restarted 5s after deploy began
    )
    assert r.ok is True


def test_bot_gateway_verification_flags_stale_gateway():
    from evolve_admin.deploy_verify import verify_bot_gateway_running_new_plugin

    began = time.time() - 30
    r = verify_bot_gateway_running_new_plugin(
        bot_id="team_bot_a",
        deploy_began_at_epoch=began,
        pid_lookup=lambda label: 11111,
        start_time_lookup=lambda pid: began - 3600,  # an hour before deploy began
    )
    assert r.ok is False


# ─────────────────────────────────────────────────────────────────────────────
# render_summary_block + all_ok
# ─────────────────────────────────────────────────────────────────────────────


def test_render_summary_includes_check_marks_for_passes():
    from evolve_admin.deploy_verify import VerificationResult, render_summary_block

    block = render_summary_block([
        VerificationResult(ok=True, summary="all good"),
        VerificationResult(ok=False, summary="bad thing", detail="line 1\nline 2"),
    ])
    assert "Verification:" in block
    assert "✓ all good" in block
    assert "✗ bad thing" in block
    # Detail lines indented under the failure
    assert "      line 1" in block
    assert "      line 2" in block


def test_render_summary_skips_detail_for_passes():
    from evolve_admin.deploy_verify import VerificationResult, render_summary_block

    block = render_summary_block([
        VerificationResult(ok=True, summary="ok one", detail="should not appear"),
    ])
    assert "should not appear" not in block


def test_all_ok_is_true_only_when_every_result_passes():
    from evolve_admin.deploy_verify import VerificationResult, all_ok

    assert all_ok([]) is True
    assert all_ok([VerificationResult(ok=True, summary="x")]) is True
    assert all_ok([
        VerificationResult(ok=True, summary="x"),
        VerificationResult(ok=False, summary="y"),
    ]) is False
