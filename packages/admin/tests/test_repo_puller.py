"""Tests for evolve_admin.repo_puller.

The puller is a small but load-bearing component: every 15min it
runs `git pull --ff-only` on the deployed evolve-repo. If it
silently destroys local commits, or if it falls into a cycle of
fail-and-retry, the whole pod's deployed code goes wrong.

These tests pin:
- Successful no-op pull (already up to date)
- Successful advance (HEAD moves forward, commits_advanced is right)
- Failure on missing repo
- Failure on non-fast-forward (with the operator-facing hint)
- Quiet log format suppresses no-op output but always shows advances/errors
- tick() wedge-detection layer: files a deduped puller-stuck issue on
  failure, surfaces a stash-count warning when stashes pile up
- Daemon auto-restart: the puller kickstarts dependent LaunchDaemons
  when the pulled diff touches code those daemons load.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from evolve_admin import repo_puller


# ── Auto-restart kill switch (tests-only default) ─────────────────────────
#
# Every test in this module runs with EVOLVE_PULLER_AUTO_RESTART=0 so the
# default ``_kickstart_daemon`` (which shells out to ``sudo /bin/launchctl``)
# never fires from a test that happens to advance HEAD across a daemon-
# triggering path. Tests that exercise the auto-restart path reset this
# explicitly via ``monkeypatch.setenv`` or pass an injected ``kickstart_fn``.

@pytest.fixture(autouse=True)
def _disable_auto_restart_by_default(monkeypatch):
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "0")


# Redirect the incident-record default (`{shared_dir}/repo-puller/incidents`)
# to the test's tmp dir so no test ever writes to the real shared dir. The
# module global is read at call time, so monkeypatching it covers every
# tick()/file_or_update call that doesn't pass an explicit incidents_dir.

@pytest.fixture(autouse=True)
def _redirect_incidents_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        repo_puller, "DEFAULT_INCIDENTS_DIR", tmp_path / "incidents",
    )


# ── pull() ────────────────────────────────────────────────────────────────


def _make_git_runner(returns: dict[str, tuple[int, str, str]]):
    """Return a fake _git that dispatches by first non-flag arg.

    `returns` maps git subcommand → (rc, stdout, stderr). E.g.
    {"rev-parse": (0, "abc123", ""), "pull": (0, "Already up to date.", "")}
    """
    def fake(repo: Path, args: list[str]) -> tuple[int, str, str]:
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in returns:
                # For rev-parse, return value can be a list (before, after)
                v = returns[arg]
                if isinstance(v, list):
                    if not v:
                        raise AssertionError(f"ran out of {arg} stub returns")
                    return v.pop(0)
                return v
            break
        raise AssertionError(f"unexpected git call: {args}")
    return fake


def test_pull_returns_failed_when_repo_missing(tmp_path: Path):
    nonexistent = tmp_path / "does-not-exist"
    result = repo_puller.pull(repo=nonexistent)
    assert result.success is False
    assert "does not exist" in result.error
    assert "FAIL" in result.steps[0]


def test_pull_no_op_when_already_up_to_date(tmp_path: Path):
    """rev-parse returns same SHA before+after → no advance."""
    sha = "a1b2c3d4e5f6789012345678901234567890abcd"
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],   # before + after
        "pull": (0, "Already up to date.", ""),
    })
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path)
    assert result.success is True
    assert result.head_before == sha
    assert result.head_after == sha
    assert result.commits_advanced == 0


def test_pull_records_advance_when_head_moves(tmp_path: Path):
    """rev-parse before != after → calls log to count advance."""
    before = "a" * 40
    after = "b" * 40
    log_output = "abc1234 commit one\ndef5678 commit two\n9876543 commit three"
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, log_output, ""),
        # Diff probe for the post-pull plugin-rebuild check; empty output
        # means no plugin paths in the diff → rebuild is skipped.
        "diff": (0, "packages/analyzer/foo.py\ndocs/CHANGELOG.md", ""),
    })
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path)
    assert result.success is True
    assert result.head_before == before
    assert result.head_after == after
    assert result.commits_advanced == 3
    assert "advanced" in result.steps[1] or "advanced" in result.steps[-1]
    # No plugin paths in the diff → rebuild not triggered
    assert result.plugin_rebuilt is False
    assert result.plugin_rebuild_error == ""


def test_pull_triggers_plugin_rebuild_when_plugin_paths_changed(tmp_path: Path):
    """If the pulled diff touches `packages/plugin/`, the puller rebuilds
    + restages so running gateways pick up the new TS code on next reload.

    Without this hook, plugin updates land in the working tree but
    openclaw keeps loading the stale staged copy from
    /Users/Shared/evolve-plugin/. Discovered 2026-05-06: dist on the mini
    was missing RecentTranscriptCapture.js → defer tool failed to register
    → operator had to ssh + rebuild manually."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        # Diff includes a packages/plugin/ path → triggers rebuild
        "diff": (0, "packages/plugin/src/tools/DeferTool.ts\nREADME.md", ""),
        # Index refresh fires after rebuild; rc=1 is acceptable too.
        "update-index": (0, "", ""),
    })
    rebuild_calls: list[bool] = []
    def fake_rebuild() -> tuple[bool, str]:
        rebuild_calls.append(True)
        return True, "rebuilt + staged"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
        )
    assert result.success is True
    assert result.plugin_rebuilt is True
    assert result.plugin_rebuild_error == ""
    assert len(rebuild_calls) == 1
    assert any("plugin" in s for s in result.steps)
    assert any("refreshed stat-cache" in s for s in result.steps)


def test_pull_skips_plugin_rebuild_when_no_plugin_paths(tmp_path: Path):
    """Diff without `packages/plugin/` paths → rebuild is NOT invoked."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/foo.py\ndocs/some.md", ""),
    })
    rebuild_calls: list[bool] = []
    def fake_rebuild() -> tuple[bool, str]:
        rebuild_calls.append(True)
        return True, "rebuilt + staged"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
        )
    assert result.success is True
    assert result.plugin_rebuilt is False
    assert len(rebuild_calls) == 0


def test_pull_records_plugin_rebuild_error_but_pull_still_success(tmp_path: Path):
    """If the rebuild raises/fails, the pull is still marked success
    (HEAD has already advanced — reverting is more disruptive than
    logging a broken stage). The error surfaces in the puller log."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/plugin/src/tools/DeferTool.ts", ""),
        # Even on rebuild failure we still refresh the index — the rebuild
        # may have written some files before crashing, leaving stat-cache
        # half-stale.
        "update-index": (1, "", "wt files differ"),
    })
    def failing_rebuild() -> tuple[bool, str]:
        return False, "tsc failed: TS5033 EACCES"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=failing_rebuild,
        )
    assert result.success is True   # HEAD moved; pull "succeeded"
    assert result.plugin_rebuilt is False
    assert "tsc failed" in result.plugin_rebuild_error
    assert any("FAIL plugin rebuild" in s for s in result.steps)


def test_pull_refreshes_stat_cache_after_plugin_rebuild(tmp_path: Path):
    """Pin the load-bearing call: after rebuild, the puller MUST run
    `git update-index --refresh` so racing readers (e.g. pod_admin_user's
    manual `git pull` minutes later) don't trip on stale stat info.

    The rebuild step (tsc → git checkout → chown/chmod cycle) updates
    file mtimes without changing content. Without this refresh, git
    reports "Your local changes to the following files would be
    overwritten" on the racing pull — exactly the wedge that surfaced
    on 2026-05-06 after PR #793 added the rebuild path."""
    before = "a" * 40
    after = "b" * 40
    git_calls: list[list[str]] = []
    def tracking_git(repo: Path, args: list[str]) -> tuple[int, str, str]:
        git_calls.append(list(args))
        if args[0] == "rev-parse":
            return (0, after if len(git_calls) > 1 else before, "")
        if args[0] == "pull":
            return (0, "Updating ...", "")
        if args[0] == "log":
            return (0, "abc commit", "")
        if args[0] == "diff":
            return (0, "packages/plugin/src/tools/DeferTool.ts", "")
        if args[0] == "update-index":
            return (0, "", "")
        raise AssertionError(f"unexpected: {args}")

    def fake_rebuild() -> tuple[bool, str]:
        return True, "rebuilt + staged"

    with patch.object(repo_puller, "_git", tracking_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
        )

    assert result.success is True
    assert result.plugin_rebuilt is True
    refresh_calls = [c for c in git_calls if c == ["update-index", "--refresh"]]
    assert len(refresh_calls) == 1, (
        f"expected exactly one update-index --refresh call, "
        f"got {len(refresh_calls)} from {git_calls}"
    )
    # Order matters: refresh comes AFTER the diff-probe (rebuild trigger)
    # and is the last git call in the rebuild branch.
    diff_idx = next(i for i, c in enumerate(git_calls) if c[0] == "diff")
    refresh_idx = next(i for i, c in enumerate(git_calls) if c[0] == "update-index")
    assert refresh_idx > diff_idx


def test_pull_no_op_does_not_trigger_plugin_rebuild(tmp_path: Path):
    """Already up-to-date pulls don't probe for plugin diffs."""
    sha = "a" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
    })
    rebuild_calls: list[bool] = []
    def fake_rebuild() -> tuple[bool, str]:
        rebuild_calls.append(True)
        return True, "rebuilt + staged"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
        )
    assert result.success is True
    assert result.plugin_rebuilt is False
    assert len(rebuild_calls) == 0


def test_format_for_log_surfaces_plugin_rebuild_status():
    """The puller log line should mention the rebuild outcome so the
    operator sees it (success or failure) on the next cycle."""
    ok_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        plugin_rebuilt=True,
    )
    out = repo_puller.format_for_log(ok_result, quiet=True)
    assert "plugin rebuilt" in out

    fail_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        plugin_rebuild_error="tsc failed: TS5033",
    )
    out = repo_puller.format_for_log(fail_result, quiet=True)
    assert "plugin rebuild failed" in out
    assert "tsc failed" in out


def test_pull_runs_install_infra_jobs_when_deploy_py_changed(tmp_path: Path):
    """If the pulled diff touches deploy.py, the puller re-runs
    install_evolve_infra_jobs so new launchd plists land + content changes
    are picked up. Without this hook, plists added in a PR (cost_watchdog
    from #906, embedding_monitor from #917, etc.) sat un-installed on the
    mini for weeks."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/evolve_admin/deploy.py\nREADME.md", ""),
    })

    from evolve_admin.deploy import DeployResult
    fake_ij_result = DeployResult(bot_id="evolve", success=True)
    fake_ij_result.steps = [
        "Installed launchd: ai.evolve.evolve.embedding_monitor",
        "Up-to-date launchd: ai.evolve.evolve.cost_watchdog (skipped reinstall)",
    ]
    install_calls: list = []

    def fake_install(evolve_dir, dry_run=False, shared_dir=None):
        install_calls.append(evolve_dir)
        return fake_ij_result

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch("evolve_admin.deploy.install_evolve_infra_jobs", side_effect=fake_install):
        result = repo_puller.pull(repo=tmp_path)

    assert result.success is True
    assert len(install_calls) == 1
    assert install_calls[0] == Path("/Users/evolve")
    assert "ai.evolve.evolve.embedding_monitor" in result.infra_jobs_installed
    assert result.infra_jobs_install_error == ""
    assert any("infra-install" in s for s in result.steps)


def test_pull_skips_install_infra_jobs_when_deploy_py_untouched(tmp_path: Path):
    """No deploy.py changes in the pulled diff → don't re-run install
    (avoids ~25 sudo calls per pull when nothing infra-related changed)."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/foo.py\ndocs/CHANGELOG.md", ""),
    })
    install_calls: list = []

    def fake_install(*args, **kwargs):
        install_calls.append(True)
        return None

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch("evolve_admin.deploy.install_evolve_infra_jobs", side_effect=fake_install):
        result = repo_puller.pull(repo=tmp_path)

    assert result.success is True
    assert install_calls == []
    assert result.infra_jobs_installed == []


def test_pull_records_install_infra_jobs_failure_but_overall_pull_succeeds(tmp_path: Path):
    """install_evolve_infra_jobs raising must NOT fail the overall pull —
    HEAD has already advanced and reverting it is more disruptive than
    logging the broken install. Operator sees the failure in the puller
    log on the next cycle."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/evolve_admin/deploy.py", ""),
    })

    def fake_install(*args, **kwargs):
        raise RuntimeError("simulated bootstrap failure")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch("evolve_admin.deploy.install_evolve_infra_jobs", side_effect=fake_install):
        result = repo_puller.pull(repo=tmp_path)

    assert result.success is True   # critical: pull succeeded
    assert "RuntimeError" in result.infra_jobs_install_error
    assert any("FAIL infra-install" in s for s in result.steps)


def test_paths_touch_infra_install_recognises_deploy_py():
    assert repo_puller._paths_touch_infra_install([
        "packages/admin/evolve_admin/deploy.py",
    ]) is True
    assert repo_puller._paths_touch_infra_install([
        "packages/admin/evolve_admin/applications/audit_scheduler.py",
    ]) is True


def test_paths_touch_infra_install_ignores_unrelated_paths():
    assert repo_puller._paths_touch_infra_install([
        "packages/analyzer/foo.py",
        "docs/CHANGELOG.md",
    ]) is False


def test_paths_touch_charters_recognises_charter_yaml():
    assert repo_puller._paths_touch_charters([
        "packages/analyzer/generators/efficiency_hawk/charter.yaml",
    ]) is True
    assert repo_puller._paths_touch_charters([
        "packages/analyzer/generators/security_warden/charter.yml",
    ]) is True


def test_paths_touch_charters_ignores_unrelated_paths():
    assert repo_puller._paths_touch_charters([
        "packages/analyzer/generators/efficiency_hawk/generator.py",
        "packages/admin/evolve_admin/deploy.py",
        "docs/CHANGELOG.md",
    ]) is False


def test_paths_touch_pyproject_recognises_admin_pyproject():
    assert repo_puller._paths_touch_pyproject([
        "packages/admin/pyproject.toml",
    ]) is True
    assert repo_puller._paths_touch_pyproject([
        "packages/admin/pyproject.toml",
        "docs/CHANGELOG.md",
    ]) is True


def test_paths_touch_pyproject_ignores_unrelated_paths():
    assert repo_puller._paths_touch_pyproject([
        "packages/analyzer/heal/runner.py",
        "packages/admin/evolve_admin/deploy.py",
        "docs/CHANGELOG.md",
    ]) is False
    # An unrelated pyproject.toml shouldn't trigger — only the admin one
    # is venv-tracked today. If/when the venv tracks more, add to the
    # _PYPROJECT_PATHS tuple in repo_puller.py AND extend this test.
    assert repo_puller._paths_touch_pyproject([
        "packages/analyzer/pyproject.toml",
    ]) is False


def test_pull_runs_pip_install_when_pyproject_changed(tmp_path: Path):
    """If the pulled diff touches packages/admin/pyproject.toml, the puller
    runs pip install -e packages/admin so new dependencies land in the venv
    before downstream daemons restart against them. Witnessed concretely
    2026-05-31 — PR #1862 added google-auth + google-api-python-client,
    the mini's bridge crashed on first call until pip ran manually."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/pyproject.toml\nREADME.md", ""),
    })
    pip_calls: list[Path] = []

    def fake_pip(repo: Path) -> tuple[bool, str]:
        pip_calls.append(repo)
        return True, "Successfully installed google-auth-2.45.0 google-api-python-client-2.197.0"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, pip_install_fn=fake_pip)

    assert result.success is True
    assert result.pip_install_attempted is True
    assert result.pip_install_ok is True
    assert "Successfully installed" in result.pip_install_info
    assert pip_calls == [tmp_path]
    assert any("pip install" in s for s in result.steps)


def test_pull_skips_pip_install_when_pyproject_untouched(tmp_path: Path):
    """No pyproject in the pulled diff → don't run pip (avoids 5s+ of
    network calls per pull when nothing dep-related changed)."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py\ndocs/CHANGELOG.md", ""),
    })
    pip_calls: list[Path] = []

    def fake_pip(repo: Path) -> tuple[bool, str]:
        pip_calls.append(repo)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, pip_install_fn=fake_pip)

    assert result.success is True
    assert result.pip_install_attempted is False
    assert pip_calls == []


def test_pull_records_pip_install_failure_but_overall_pull_succeeds(tmp_path: Path):
    """pip install failure (network down, version conflict, etc.) must NOT
    fail the overall pull. Operator sees the failure in the puller log + the
    structured pip_install_info / pip_install_ok=False fields and can re-run
    pip manually. HEAD has already advanced; reverting it is more disruptive."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/pyproject.toml", ""),
    })

    def failing_pip(repo: Path) -> tuple[bool, str]:
        return False, "rc=1: ERROR: Could not find a version that satisfies the requirement xyzzy"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, pip_install_fn=failing_pip)

    assert result.success is True
    assert result.pip_install_attempted is True
    assert result.pip_install_ok is False
    assert "xyzzy" in result.pip_install_info
    assert any("FAIL pip install" in s for s in result.steps)


def test_pip_install_grant_present_in_evolve_sudoers():
    """The auto-pip-install hook runs ``sudo /Users/Shared/evolve-venv/bin/pip
    install -e /Users/Shared/evolve-repo/packages/admin`` from the puller daemon
    (which runs as the evolve user). Without the matching narrow sudoers grant
    in ``_render_evolve_sudoers``, sudo fails with 'evolve is not in the sudoers
    file' and the hook is a no-op. This test pins the grant so a future refactor
    can't drop it without surfacing here."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers
    content = _render_evolve_sudoers()
    assert content is not None, "openclaw not discoverable at test time"
    needle = (
        "evolve ALL=(root) NOPASSWD: "
        "/Users/Shared/evolve-venv/bin/pip install -e "
        "/Users/Shared/evolve-repo/packages/admin"
    )
    assert needle in content, (
        f"Expected sudoers grant for the auto-pip-install hook is missing. "
        f"Add this line in setup_wizard._render_evolve_sudoers:\n  {needle}"
    )


def test_pull_pip_install_runs_before_daemon_restart(tmp_path: Path, monkeypatch):
    """Order matters: pip install must complete BEFORE daemons restart.
    Otherwise the daemons reload, fail to import the new modules, and
    end up in a crash loop AND with stale code. Capture the call order
    via a shared sequence list."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/pyproject.toml\npackages/admin/evolve_admin/server.py", ""),
    })
    sequence: list[str] = []

    def fake_pip(repo: Path) -> tuple[bool, str]:
        sequence.append("pip")
        return True, "ok"

    def fake_kick(label: str) -> tuple[bool, str]:
        sequence.append(f"kick:{label}")
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path, pip_install_fn=fake_pip, kickstart_fn=fake_kick,
        )

    assert result.success is True
    assert "pip" in sequence
    assert any(s.startswith("kick:") for s in sequence)
    pip_idx = sequence.index("pip")
    first_kick_idx = next(i for i, s in enumerate(sequence) if s.startswith("kick:"))
    assert pip_idx < first_kick_idx, (
        f"pip install must precede daemon kickstart; sequence={sequence}"
    )


def test_pull_bumps_charter_fingerprints_when_charter_changed(tmp_path: Path):
    """When the pulled diff includes a charter.yaml, the bump runs and updates
    the stale record — exactly the pattern that left three generators broken
    in May 2026 (efficiency_hawk, security_warden, sysadmin_watchdog)."""
    import json as _json

    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/generators/efficiency_hawk/charter.yaml", ""),
    })

    # Set up a fake repo + shared_dir with one charter and one stale record.
    charter_content = "id: efficiency_hawk\ntype: optimizer\n"
    new_fp = repo_puller._compute_charter_fingerprint(charter_content)
    old_fp = "deadbeef" * 8

    gen_dir = tmp_path / "packages" / "analyzer" / "generators" / "efficiency_hawk"
    gen_dir.mkdir(parents=True)
    (gen_dir / "charter.yaml").write_text(charter_content)

    shared = tmp_path / "shared"
    records_dir = shared / "generators"
    records_dir.mkdir(parents=True)
    record = {"id": "efficiency_hawk", "charter_fingerprint": old_fp, "status": "active"}
    (records_dir / "efficiency_hawk.json").write_text(_json.dumps(record))

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, shared_dir=shared)

    assert result.success is True
    assert result.charter_fingerprints_bumped == 1
    assert result.charter_fingerprint_bump_error == ""
    assert any("charter-bump: 1 updated" in s for s in result.steps)

    saved = _json.loads((records_dir / "efficiency_hawk.json").read_text())
    assert saved["charter_fingerprint"] == new_fp


def test_pull_skips_charter_bump_when_no_charter_in_diff(tmp_path: Path):
    """No charter.yaml in the diff → bump never runs."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/generators/efficiency_hawk/generator.py", ""),
    })

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path)

    assert result.success is True
    assert result.charter_fingerprints_bumped == 0
    assert not any("charter-bump" in s for s in result.steps)


def test_pull_charter_bump_error_does_not_fail_pull(tmp_path: Path):
    """A failure inside _bump_charter_fingerprints must NOT fail the overall
    pull — HEAD has already advanced."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/generators/efficiency_hawk/charter.yaml", ""),
    })

    def boom(repo, shared_dir):
        raise OSError("disk full")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "_bump_charter_fingerprints", side_effect=boom):
        result = repo_puller.pull(repo=tmp_path)

    assert result.success is True
    assert "OSError" in result.charter_fingerprint_bump_error
    assert any("FAIL charter-bump" in s for s in result.steps)


def test_pull_fails_on_non_fast_forward_with_hint(tmp_path: Path):
    """The most operationally important failure: someone made a local
    commit on mini that origin doesn't have. --ff-only refuses; we
    surface a clear hint instead of letting git noise be the only signal."""
    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path)
    assert result.success is False
    assert "non-fast-forward" in result.error or "Not possible" in result.error
    # The HINT is what an operator scanning logs would notice
    hint_lines = [s for s in result.steps if "HINT" in s]
    assert len(hint_lines) == 1
    assert "do not force-pull" in hint_lines[0]


def test_pull_fails_cleanly_on_rev_parse_error(tmp_path: Path):
    """If git rev-parse HEAD fails on a REAL git repo (e.g. detached HEAD
    weirdness / bad revision), don't crash — return error. The next tick
    will retry. The repo carries a `.git` so this is the corrupt-repo path,
    distinct from the non-git no-op (test_pull_no_op_when_not_a_git_repo)."""
    (tmp_path / ".git").mkdir()
    fake = _make_git_runner({
        "rev-parse": (128, "", "fatal: bad revision"),
    })
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(repo=tmp_path)
    assert result.success is False
    assert "rev-parse HEAD failed" in result.error


def test_pull_no_op_when_not_a_git_repo(tmp_path: Path):
    """Single-box / tarball-staged pod: the deploy checkout exists but is not
    a git working tree (no `.git`). `rev-parse HEAD` fails, but instead of
    wedging the puller we no-op cleanly (success=True, skipped_not_git=True,
    exit 0) so health stays green. Round-3 W10-F #8c single-VPS decision."""
    fake = _make_git_runner({
        "rev-parse": (128, "", "fatal: not a git repository"),
    })
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(repo=tmp_path)
    assert result.success is True
    assert result.skipped_not_git is True
    assert not result.error
    assert any("not a git working tree" in s for s in result.steps)


# ── format_for_log ────────────────────────────────────────────────────────


def test_format_quiet_suppresses_no_op():
    r = repo_puller.PullResult(success=True, head_before="a"*40, head_after="a"*40)
    assert repo_puller.format_for_log(r, quiet=True) == ""


def test_format_non_quiet_shows_no_op():
    r = repo_puller.PullResult(success=True, head_before="a"*40, head_after="a"*40)
    out = repo_puller.format_for_log(r, quiet=False)
    assert "up to date" in out
    assert "aaaaaaaa" in out


def test_format_always_shows_error_even_in_quiet():
    """Errors must surface whether or not we're in quiet mode."""
    r = repo_puller.PullResult(success=False, error="git pull failed: fatal: ...")
    out = repo_puller.format_for_log(r, quiet=True)
    assert "ERROR" in out
    assert "git pull failed" in out


def test_format_always_shows_advance_even_in_quiet():
    """An advance is interesting; never suppress."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=3,
        log_summary="abc commit\ndef commit\n123 commit",
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "advanced" in out
    assert "3 commits" in out
    assert "abc commit" in out


# ── ensure_deploy_key ─────────────────────────────────────────────────────


def test_ensure_deploy_key_generates_key_when_missing(tmp_path: Path, monkeypatch):
    """Fresh setup: no key file → ssh-keygen runs → key + pub written."""
    key_path = tmp_path / "evolve-repo"
    ssh_config = tmp_path / "config"

    monkeypatch.setattr(repo_puller, "EVOLVE_SSH_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        # Simulate ssh-keygen creating the files
        if "/usr/bin/ssh-keygen" in cmd:
            key_path.write_text("PRIVATE")
            (key_path.with_suffix(".pub")).write_text("ssh-ed25519 AAAA... evolve@mini")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(repo_puller.subprocess, "run", fake_run):
        result = repo_puller.ensure_deploy_key(
            key_path=key_path, ssh_config=ssh_config, test_auth=False,
        )

    assert result.success is True
    assert result.key_generated is True
    assert result.public_key.startswith("ssh-ed25519")
    assert result.config_updated is True
    assert ssh_config.exists()
    assert "evolve-repo" in ssh_config.read_text()


def test_ensure_deploy_key_skips_keygen_when_key_present(tmp_path: Path, monkeypatch):
    """Idempotent: existing key is preserved; ssh-keygen NOT called."""
    key_path = tmp_path / "evolve-repo"
    pub_path = key_path.with_suffix(".pub")
    ssh_config = tmp_path / "config"
    key_path.write_text("EXISTING")
    pub_path.write_text("ssh-ed25519 BBBB... existing")

    monkeypatch.setattr(repo_puller, "EVOLVE_SSH_DIR", tmp_path)

    calls: list[list[str]] = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(repo_puller.subprocess, "run", fake_run):
        result = repo_puller.ensure_deploy_key(
            key_path=key_path, ssh_config=ssh_config, test_auth=False,
        )

    assert result.success is True
    assert result.key_generated is False
    assert result.public_key == "ssh-ed25519 BBBB... existing"
    assert key_path.read_text() == "EXISTING"
    assert not any("ssh-keygen" in arg for cmd in calls for arg in cmd)


def test_ensure_deploy_key_idempotent_on_ssh_config(tmp_path: Path, monkeypatch):
    """Re-running with a config that already references evolve-repo doesn't
    duplicate the entry. Marker check uses substring 'evolve-repo'."""
    key_path = tmp_path / "evolve-repo"
    pub_path = key_path.with_suffix(".pub")
    ssh_config = tmp_path / "config"
    key_path.write_text("X")
    pub_path.write_text("ssh-ed25519 X x")
    ssh_config.write_text("Host github.com\n    IdentityFile ~/.ssh/evolve-repo\n")

    monkeypatch.setattr(repo_puller, "EVOLVE_SSH_DIR", tmp_path)

    with patch.object(repo_puller.subprocess, "run",
                      return_value=type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()):
        result = repo_puller.ensure_deploy_key(
            key_path=key_path, ssh_config=ssh_config, test_auth=False,
        )

    assert result.success is True
    assert result.config_updated is False
    assert ssh_config.read_text() == "Host github.com\n    IdentityFile ~/.ssh/evolve-repo\n"


def test_ensure_deploy_key_auth_test_recognizes_github_success(tmp_path: Path, monkeypatch):
    """GitHub returns 'Hi <user>! You've successfully authenticated...' with
    exit code 1 (because no shell access). The check is on the substring,
    not the exit code."""
    key_path = tmp_path / "evolve-repo"
    pub_path = key_path.with_suffix(".pub")
    key_path.write_text("X")
    pub_path.write_text("ssh-ed25519 X x")
    monkeypatch.setattr(repo_puller, "EVOLVE_SSH_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        if "ssh" in cmd[0] or (len(cmd) > 1 and cmd[1] == "-u"):
            return type("R", (), {
                "returncode": 1,
                "stdout": "",
                "stderr": "Hi evolve-ops/evolve! You've successfully authenticated, but GitHub does not provide shell access.",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(repo_puller.subprocess, "run", fake_run):
        result = repo_puller.ensure_deploy_key(
            key_path=key_path, ssh_config=tmp_path / "config", test_auth=True,
        )

    assert result.auth_test_ok is True
    assert "successfully authenticated" in result.auth_test_msg


def test_ensure_deploy_key_auth_test_recognizes_permission_denied(tmp_path: Path, monkeypatch):
    """Public key not registered yet → ssh -T returns 'Permission denied (publickey).'
    Surfaced as auth_test_ok=False so the operator instructions kick in."""
    key_path = tmp_path / "evolve-repo"
    pub_path = key_path.with_suffix(".pub")
    key_path.write_text("X")
    pub_path.write_text("ssh-ed25519 X x")
    monkeypatch.setattr(repo_puller, "EVOLVE_SSH_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        if "ssh" in cmd[0] or (len(cmd) > 1 and cmd[1] == "-u"):
            return type("R", (), {
                "returncode": 255,
                "stdout": "",
                "stderr": "git@github.com: Permission denied (publickey).",
            })()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(repo_puller.subprocess, "run", fake_run):
        result = repo_puller.ensure_deploy_key(
            key_path=key_path, ssh_config=tmp_path / "config", test_auth=True,
        )

    assert result.auth_test_ok is False
    assert "Permission denied" in result.auth_test_msg


# ── format_deploy_key_instructions ────────────────────────────────────────


def test_format_instructions_when_auth_passes():
    """Auth verified → terse confirmation, no instructions to follow."""
    r = repo_puller.DeployKeyResult(
        success=True, auth_test_ok=True, public_key="ssh-ed25519 X x"
    )
    out = repo_puller.format_deploy_key_instructions(r, repo_url="https://github.com/evolve-ops/evolve")
    assert "Auth verified" in out
    assert "no further action" in out.lower()


def test_format_instructions_when_auth_fails_includes_full_steps():
    """Auth failed → walk the operator through adding the key to GitHub."""
    pub = "ssh-ed25519 AAAA evolve@mini"
    r = repo_puller.DeployKeyResult(success=True, auth_test_ok=False, public_key=pub)
    out = repo_puller.format_deploy_key_instructions(r, repo_url="https://github.com/evolve-ops/evolve")
    assert pub in out
    assert "https://github.com/evolve-ops/evolve/settings/keys/new" in out
    assert "READ-ONLY" in out
    assert "Allow write access" in out


# ── _git wrapper ──────────────────────────────────────────────────────────


def test_git_wrapper_includes_safe_directory(tmp_path: Path):
    """The safe.directory config must be set or git refuses on
    cross-user-owned repos. Verify the actual command line."""
    captured: dict = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "OK", "stderr": ""})()
    with patch.object(repo_puller.subprocess, "run", fake_run):
        rc, out, err = repo_puller._git(tmp_path, ["status"])
    assert rc == 0
    cmd = captured["cmd"]
    # Must include `-c safe.directory=<repo>`
    assert "-c" in cmd
    safe_idx = cmd.index("-c") + 1
    assert cmd[safe_idx].startswith("safe.directory=")
    assert str(tmp_path) in cmd[safe_idx]
    # Must include `-C <repo>`
    assert "-C" in cmd


# ── puller-stuck issue filing (wedge detection) ───────────────────────────


def _make_repo_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (repo, incidents_dir) rooted at the test's tmp dir.

    `tmp_path / "incidents"` matches what the autouse
    ``_redirect_incidents_dir`` fixture patches ``DEFAULT_INCIDENTS_DIR``
    to, so the second value is where default-path filing actually lands."""
    incidents = tmp_path / "incidents"
    incidents.mkdir(parents=True, exist_ok=True)
    return tmp_path, incidents


def test_file_or_update_creates_new_issue_when_none_exists(tmp_path: Path):
    """First wedge of the day: nothing in the incidents dir matches → mint a new
    finding with kind: puller-stuck and a Recurrences row recording this
    failure. The unstuck recipe must appear so an operator can act without
    re-deriving it."""
    repo, incidents = _make_repo_layout(tmp_path)
    now = dt.datetime(2026, 5, 3, 7, 0, 58, tzinfo=dt.timezone.utc)
    err = "fatal: Not possible to fast-forward, aborting."

    path, was_new = repo_puller.file_or_update_puller_stuck_issue(
        error=err, now=now,
    )

    assert was_new is True
    assert path.exists()
    text = path.read_text()
    assert "kind: puller-stuck" in text
    assert "first_seen: 2026-05-03T07:00:58Z" in text
    assert "last_seen: 2026-05-03T07:00:58Z" in text
    assert "## Recurrences" in text
    assert "fatal: Not possible to fast-forward" in text
    # Recipe stays in the body so the operator doesn't have to re-derive it.
    # No upstream_touched payload provided → default stash recipe.
    assert "git stash push" in text
    # The discard-recipe markers must not leak into the stash branch — that
    # would cross the wires and tell the operator to discard their real WIP.
    assert "git checkout -- " not in text


def test_file_or_update_renders_discard_recipe_when_upstream_touched(tmp_path: Path):
    """The 2026-06-06 case the original recipe got wrong: blocking path was
    already touched by an upstream commit (the local diff was a duplicate of
    a merged PR). When ``upstream_touched`` is provided, the recipe must
    switch from ``git stash`` (which would conflict on pop because the change
    is on both sides) to ``git diff origin/main -- <path>`` to confirm and
    ``git checkout -- <path>`` to discard local.

    Listing the upstream commits inline is what lets the operator spot the
    duplicate-PR case at a glance — without it, they'd have to re-run
    ``git log`` themselves before trusting the recipe.
    """
    repo, _ = _make_repo_layout(tmp_path)
    now = dt.datetime(2026, 6, 6, 23, 30, 0, tzinfo=dt.timezone.utc)
    err = (
        "error: Your local changes to the following files would be "
        "overwritten by merge:\n"
        "\tpackages/admin/evolve_admin/deploy.py\n"
        "Aborting"
    )
    upstream = {
        "packages/admin/evolve_admin/deploy.py": [
            "d98597c5 fix(perms): add config_intents to evo write-ACL contract (#2331)",
        ],
    }

    path, was_new = repo_puller.file_or_update_puller_stuck_issue(
        error=err, now=now, upstream_touched=upstream,
    )

    assert was_new is True
    text = path.read_text()
    # Discard recipe — the right fix when upstream already carries the change.
    assert "git diff origin/main -- packages/admin/evolve_admin/deploy.py" in text
    assert "git checkout -- packages/admin/evolve_admin/deploy.py" in text
    assert "git pull --ff-only" in text
    # The stash recipe must NOT be the recommended path here — that's the
    # exact confusion the 2026-06-06 incident exposed.
    assert "git stash push" not in text
    # The upstream commit is listed inline so the operator can sanity-check
    # without re-running ``git log`` themselves.
    assert "d98597c5 fix(perms): add config_intents" in text


def test_file_or_update_appends_recurrence_within_window(tmp_path: Path):
    """Within the dedup window: bump last_seen and add a Recurrences row,
    DO NOT mint a fresh issue. This is what stops 96 puller-stuck issues
    from accumulating per failure-day."""
    repo, _ = _make_repo_layout(tmp_path)
    t1 = dt.datetime(2026, 5, 3, 7, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 3, 7, 30, 0, tzinfo=dt.timezone.utc)

    path1, was_new_1 = repo_puller.file_or_update_puller_stuck_issue(
        error="first failure", now=t1,
    )
    path2, was_new_2 = repo_puller.file_or_update_puller_stuck_issue(
        error="second failure", now=t2,
    )

    assert was_new_1 is True
    assert was_new_2 is False
    assert path1 == path2
    text = path1.read_text()
    # last_seen advanced
    assert "last_seen: 2026-05-03T07:30:00Z" in text
    assert "last_seen: 2026-05-03T07:00:00Z" not in text
    # both errors visible in Recurrences
    assert "first failure" in text
    assert "second failure" in text


def test_file_or_update_creates_new_issue_after_window_lapses(tmp_path: Path):
    """Outside the dedup window: mint a new issue rather than zombie-bumping
    the stale one. The stale issue stays put as a historical record."""
    repo, incidents = _make_repo_layout(tmp_path)
    t1 = dt.datetime(2026, 5, 3, 7, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 3, 9, 0, 0, tzinfo=dt.timezone.utc)   # 2h later

    path1, was_new_1 = repo_puller.file_or_update_puller_stuck_issue(
        error="first cluster", now=t1,
    )
    path2, was_new_2 = repo_puller.file_or_update_puller_stuck_issue(
        error="second cluster", now=t2,
    )

    assert was_new_1 is True
    assert was_new_2 is True
    assert path1 != path2
    # Both files now exist with their respective ids
    assert {p.name for p in incidents.iterdir()} == {path1.name, path2.name}


def test_file_or_update_skips_non_puller_stuck_issues(tmp_path: Path):
    """A pre-existing non-puller-stuck issue with a recent last_seen must
    not be mistaken for a puller-stuck dedup target. Otherwise we'd silently
    swallow new puller wedges whenever any other issue happens to be fresh."""
    repo, incidents = _make_repo_layout(tmp_path)
    now = dt.datetime(2026, 5, 3, 7, 0, 0, tzinfo=dt.timezone.utc)
    decoy = incidents / "2026-05-03-099-decoy.md"
    decoy.write_text(
        "---\n"
        "id: 2026-05-03-099\n"
        "kind: regression-finding\n"
        "title: \"unrelated\"\n"
        "first_seen: 2026-05-03T06:50:00Z\n"
        "last_seen: 2026-05-03T06:50:00Z\n"
        "---\n\n"
        "## Symptom\n\nUnrelated.\n"
    )

    path, was_new = repo_puller.file_or_update_puller_stuck_issue(
        error="boom", now=now,
    )
    assert was_new is True
    assert path != decoy
    # The decoy is unchanged.
    assert decoy.read_text().count("last_seen: 2026-05-03T06:50:00Z") == 1


def test_next_issue_id_increments_past_existing_today(tmp_path: Path):
    """Fresh ids must skip past any NNN already used today, so a puller-stuck
    issue minted at the same wall-clock as a worker-filed regression doesn't
    collide."""
    repo, incidents = _make_repo_layout(tmp_path)
    (incidents / "2026-05-03-005.md").write_text("---\nid: 2026-05-03-005\n---\n")
    (incidents / "2026-05-03-007-foo.md").write_text("---\nid: 2026-05-03-007\n---\n")
    now = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    new_id = repo_puller._next_issue_id(incidents, now)
    assert new_id == "2026-05-03-008"


# ── tick(): integration of pull + side effects ────────────────────────────


def test_tick_files_issue_on_pull_failure(tmp_path: Path):
    """tick() wraps pull(); on failure it must file a puller-stuck issue
    AND surface a non-zero result. This is the path that stops the silent
    wedge — without it, a failure is just one more line of noise in the log."""
    repo, _ = _make_repo_layout(tmp_path)
    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    now = dt.datetime(2026, 5, 3, 7, 0, 58, tzinfo=dt.timezone.utc)
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, now=now)

    assert result.pull.success is False
    assert result.issue_path is not None
    assert result.issue_was_new is True
    assert result.issue_path.exists()
    assert "kind: puller-stuck" in result.issue_path.read_text()


def test_tick_appends_recurrence_on_repeat_failure(tmp_path: Path):
    """Two failed ticks within the dedup window → one issue, two recurrences.
    Pinning this prevents a regression where dedup silently breaks and the
    queue floods."""
    repo, _ = _make_repo_layout(tmp_path)
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)

    t1 = dt.datetime(2026, 5, 3, 7, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 3, 7, 15, 0, tzinfo=dt.timezone.utc)

    # Each tick runs pull() once, which calls rev-parse once + pull once.
    with patch.object(repo_puller, "_git",
                      _make_git_runner({
                          "rev-parse": (0, "a" * 40, ""),
                          "pull": (1, "", "fatal: refusing to merge unrelated histories"),
                      })), \
         patch.object(Path, "exists", fake_exists):
        r1 = repo_puller.tick(repo=repo, now=t1)
    with patch.object(repo_puller, "_git",
                      _make_git_runner({
                          "rev-parse": (0, "a" * 40, ""),
                          "pull": (1, "", "fatal: refusing to merge unrelated histories"),
                      })), \
         patch.object(Path, "exists", fake_exists):
        r2 = repo_puller.tick(repo=repo, now=t2)

    assert r1.issue_was_new is True
    assert r2.issue_was_new is False
    assert r1.issue_path == r2.issue_path
    text = r1.issue_path.read_text()
    assert text.count("- 2026-05-03T07:00:00Z") == 1
    assert text.count("- 2026-05-03T07:15:00Z") == 1


def test_tick_counts_stashes_on_success(tmp_path: Path):
    """Successful pull → stash_count populated, no issue filed. The count
    is one of the few low-cost ways to spot the on-mini-edits-then-stash
    pattern that hides real wedges (the 2026-05-03 incident)."""
    repo, _ = _make_repo_layout(tmp_path)
    sha = "a" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
        "stash": (0, "stash@{0}: WIP\nstash@{1}: WIP\n", ""),
    })
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo)
    assert result.pull.success is True
    assert result.stash_count == 2
    assert result.stash_warning is False
    assert result.issue_path is None


def test_tick_warns_when_stashes_exceed_threshold(tmp_path: Path):
    """8 stashes on the mini was the trigger that uncovered this whole
    finding. Warn at >3 so it gets surfaced before it grows further."""
    repo, _ = _make_repo_layout(tmp_path)
    sha = "a" * 40
    stash_out = "\n".join(f"stash@{{{i}}}: WIP {i}" for i in range(8))
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
        "stash": (0, stash_out, ""),
    })
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo)
    assert result.stash_count == 8
    assert result.stash_warning is True
    out = repo_puller.format_tick_for_log(result, quiet=True)
    assert "WARNING" in out
    assert "8 stashes" in out


def test_tick_log_format_mentions_issue_action(tmp_path: Path):
    """On failure, the log line must say which issue file the tick
    touched — operators reading the log scrollback want to know if a
    new finding was minted vs a recurrence appended."""
    r = repo_puller.TickResult(
        pull=repo_puller.PullResult(success=False, error="boom"),
        issue_path=Path("incidents/2026-05-03-007-repo-puller-wedged.md"),
        issue_was_new=True,
    )
    out = repo_puller.format_tick_for_log(r, quiet=True)
    assert "filed" in out
    assert "2026-05-03-007-repo-puller-wedged.md" in out

    r2 = repo_puller.TickResult(
        pull=repo_puller.PullResult(success=False, error="boom"),
        issue_path=Path("incidents/2026-05-03-007-repo-puller-wedged.md"),
        issue_was_new=False,
    )
    out2 = repo_puller.format_tick_for_log(r2, quiet=True)
    assert "appended recurrence" in out2


def test_tick_does_not_raise_when_issue_dir_unwritable(tmp_path: Path):
    """The daemon's correctness is more important than the issue-filing
    side effect. If we can't write the issue (e.g. dir owned by the wrong
    user), we still want the tick to exit cleanly with the pull failure
    surfaced — but the filing failure must be RECORDED, not swallowed:
    a failed filing also suppresses the wedge notification (it needs the
    record path), so without the issue_error trace + log line the only
    evidence would be a file that isn't there."""
    repo, _ = _make_repo_layout(tmp_path)
    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: lock"),
    })
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)

    def boom(*a, **kw):
        raise PermissionError("read-only fs")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists), \
         patch.object(repo_puller, "file_or_update_puller_stuck_issue", boom):
        result = repo_puller.tick(repo=repo)

    assert result.pull.success is False
    assert result.issue_path is None
    assert "PermissionError" in result.issue_error
    assert "read-only fs" in result.issue_error
    out = repo_puller.format_tick_for_log(result, quiet=True)
    assert "incident filing FAILED" in out
    assert "read-only fs" in out


def test_count_stashes_returns_zero_when_git_fails(tmp_path: Path):
    """Soft signal: a broken `git stash list` must not crash the puller
    or distort the daemon's exit code. Return 0 and move on."""
    fake = _make_git_runner({"stash": (128, "", "fatal: not a git repo")})
    with patch.object(repo_puller, "_git", fake):
        assert repo_puller.count_stashes(tmp_path) == 0


# ── _check_repo_puller_freshness() — health-checker integration ───────────
#
# These tests pin the contract that operator-visible signals fire when the
# puller wedges. The publickey wedge of 2026-04-29..2026-05-04 went days
# unnoticed because the puller's own issue files lived under issues/open/,
# not in the routine `evolve-admin health` output. The check below is the
# durable fix; these tests guard its three failure shapes.


def _write_log(path: Path, content: str) -> Path:
    """Helper: drop a synthetic puller log on disk for the freshness check
    to consume. Used by every test in this group — separate so the body of
    each test stays focused on the contract being asserted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_freshness_warns_when_log_missing(tmp_path: Path):
    from evolve_admin import health
    report = health.HealthReport()
    health._check_repo_puller_freshness(
        report, log_path=tmp_path / "no-such-log.log"
    )
    assert len(report.checks) == 1
    c = report.checks[0]
    assert c.status == health.WARN
    assert "missing" in c.detail.lower()
    assert c.fix_args == {
        "action": "launchctl_kickstart",
        "label": "ai.evolve.evolve.repo-puller",
    }


def test_freshness_warns_when_log_stale(tmp_path: Path):
    """File mtime older than threshold → daemon unloaded."""
    import os
    from evolve_admin import health
    log = _write_log(
        tmp_path / "repo-puller.log",
        "[repo-puller] stashes=0\n",
    )
    # Push mtime to 4 hours ago (well past the 90-min threshold).
    old = (tmp_path.stat().st_mtime) - (4 * 60 * 60)
    os.utime(log, (old, old))

    report = health.HealthReport()
    health._check_repo_puller_freshness(report, log_path=log)
    assert len(report.checks) == 1
    c = report.checks[0]
    assert c.status == health.WARN
    assert "min" in c.detail
    assert c.name == "repo-puller:stale"


def test_freshness_fails_when_log_shows_recent_error_and_no_success(tmp_path: Path):
    """The 2026-04-29 publickey wedge shape: every tick is ERROR, no
    success line in the tail. This must be FAIL (not WARN) and surface
    the deploy-key fix command."""
    from evolve_admin import health
    log = _write_log(
        tmp_path / "repo-puller.log",
        "[repo-puller] ERROR: pull --ff-only failed: Permission denied (publickey).\n"
        "[repo-puller] filed 2026-04-29-001-repo-puller-wedged.md\n"
        "[repo-puller] ERROR: pull --ff-only failed: Permission denied (publickey).\n"
        "[repo-puller] appended recurrence to 2026-04-29-001-repo-puller-wedged.md\n",
    )
    report = health.HealthReport()
    health._check_repo_puller_freshness(report, log_path=log)
    assert len(report.checks) == 1
    c = report.checks[0]
    assert c.status == health.FAIL
    assert "drifting from origin/main" in c.detail
    assert "Permission denied" in c.detail
    assert "--setup-key" in (c.fix_cmd or "")


def test_freshness_passes_on_recent_success_after_old_error(tmp_path: Path):
    """Recovery path: the log historically had an ERROR line but the
    most recent tick succeeded (`stashes=N` line). This must PASS — the
    puller has self-recovered, no operator action needed.

    Guards against regressions where we'd e.g. greedily match any ERROR
    in the tail and stay red even after recovery."""
    from evolve_admin import health
    log = _write_log(
        tmp_path / "repo-puller.log",
        "[repo-puller] ERROR: pull --ff-only failed: Permission denied (publickey).\n"
        "[repo-puller] stashes=0\n"
        "[repo-puller] stashes=0\n",
    )
    report = health.HealthReport()
    health._check_repo_puller_freshness(report, log_path=log)
    assert len(report.checks) == 1
    c = report.checks[0]
    assert c.status == health.PASS
    assert c.name == "repo-puller:freshness"


# ── untracked-file conflict recovery ──────────────────────────────────────
#
# Real-world failure (2026-05-05): the puller failed for hours because
# `git pull --ff-only` refused to overwrite untracked files left behind
# by an off-checkout edit (Cowork / a misplaced Claude session). The
# files were byte-identical to what origin was about to install — so a
# delete + retry would have worked. These tests pin that recovery path.


_UNTRACKED_ERR_TEMPLATE = (
    "From github.com:evolve-ops/evolve\n"
    " * branch              main       -> FETCH_HEAD\n"
    "error: The following untracked working tree files would be "
    "overwritten by merge:\n"
    "{paths}"
    "Please move or remove them before you merge.\n"
    "Aborting"
)


def _untracked_err(*relpaths: str) -> str:
    body = "".join(f"\t{p}\n" for p in relpaths)
    return _UNTRACKED_ERR_TEMPLATE.format(paths=body)


def test_parse_untracked_conflict_files_handles_real_git_output():
    """The parser must pull every tab-indented path out of the marker
    block and stop at the next non-tab line."""
    err = _untracked_err(
        "packages/analyzer/extract_tuples.py",
        "packages/analyzer/observations/llm_extractor.py",
    )
    files = repo_puller._parse_untracked_conflict_files(err)
    assert files == [
        "packages/analyzer/extract_tuples.py",
        "packages/analyzer/observations/llm_extractor.py",
    ]


def test_parse_untracked_conflict_files_returns_empty_when_marker_missing():
    """Defensive: unrelated errors must not produce phantom paths."""
    err = "fatal: Not possible to fast-forward, aborting."
    assert repo_puller._parse_untracked_conflict_files(err) == []


def test_pull_recovers_when_untracked_files_match_origin(tmp_path: Path):
    """The 2026-05-05 case exactly: the offending untracked files are
    byte-identical to what `<remote>/<branch>:<path>` will install. The
    puller should delete them and retry the pull, ending up successful
    with deleted_identical populated and quarantined empty."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    qdir = tmp_path / "quarantine"

    rel = "packages/analyzer/extract_tuples.py"
    f = repo / rel
    f.parent.mkdir(parents=True)
    content = "# matches origin exactly\nprint('hi')\n"
    f.write_text(content)

    before, after = "a" * 40, "b" * 40
    err = _untracked_err(rel)
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": [(1, "", err), (0, "Updating ...", "")],
        "show": (0, content, ""),   # remote file == local file
        "log": (0, "abc commit\n", ""),
        "diff": (0, "", ""),   # no plugin paths → rebuild skipped
    })
    now = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(
            repo=repo, quarantine_root=qdir, now=now,
        )

    assert result.success is True
    assert result.deleted_identical == [rel]
    assert result.quarantined == []
    assert not f.exists(), "deleted-identical file should be gone"
    assert not qdir.exists(), "no quarantine dir when nothing diverged"


def test_pull_quarantines_divergent_untracked_files(tmp_path: Path):
    """When the local content differs from origin (real WIP, not just
    accidental duplication), preserve the file under the quarantine
    timestamp dir rather than deleting it."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    qdir = tmp_path / "quarantine"

    rel = "packages/analyzer/extract_tuples.py"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("# local WIP, NOT what origin has\n")

    before, after = "a" * 40, "b" * 40
    err = _untracked_err(rel)
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": [(1, "", err), (0, "Updating ...", "")],
        "show": (0, "# what origin would install\n", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "", ""),
    })
    now = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(
            repo=repo, quarantine_root=qdir, now=now,
        )

    assert result.success is True
    assert result.quarantined == [rel]
    assert result.deleted_identical == []
    assert not f.exists(), "original location must be cleared"
    quarantined = qdir / "20260505T170000Z" / rel
    assert quarantined.exists(), f"file should be at {quarantined}"
    assert quarantined.read_text() == "# local WIP, NOT what origin has\n"
    assert result.quarantine_dir == str(qdir / "20260505T170000Z")


def test_pull_handles_mixed_identical_and_divergent_files(tmp_path: Path):
    """Two offending files in one error: one matches origin (delete),
    one diverges (quarantine). Both must be swept before the retry, and
    each must end up in the right bucket on the result."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    qdir = tmp_path / "quarantine"

    rel_same = "packages/a/match.py"
    rel_diff = "packages/b/diverge.py"
    (repo / rel_same).parent.mkdir(parents=True)
    (repo / rel_diff).parent.mkdir(parents=True)
    same_content = "same content\n"
    (repo / rel_same).write_text(same_content)
    (repo / rel_diff).write_text("local-only\n")

    err = _untracked_err(rel_same, rel_diff)
    # `show` is called per-path; return same_content for the first path
    # (matches), divergent content for the second (preserved). Order
    # matches the parser's output order.
    fake = _make_git_runner({
        "rev-parse": [(0, "a" * 40, ""), (0, "b" * 40, "")],
        "pull": [(1, "", err), (0, "Updating ...", "")],
        "show": [(0, same_content, ""), (0, "origin-only\n", "")],
        "log": (0, "abc commit\n", ""),
        "diff": (0, "", ""),
    })
    now = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(
            repo=repo, quarantine_root=qdir, now=now,
        )

    assert result.success is True
    assert result.deleted_identical == [rel_same]
    assert result.quarantined == [rel_diff]
    assert not (repo / rel_same).exists()
    assert not (repo / rel_diff).exists()
    assert (qdir / "20260505T170000Z" / rel_diff).exists()


def test_pull_fails_cleanly_when_retry_after_sweep_fails(tmp_path: Path):
    """If sweeping the untracked files isn't enough (e.g. additional
    untracked files surface, or a different error appears), keep the
    failure surface — but the result should reflect what we did sweep
    so the issue body has a useful audit trail."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    qdir = tmp_path / "quarantine"

    rel = "packages/x.py"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("local\n")

    err = _untracked_err(rel)
    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": [(1, "", err),
                 (1, "", "fatal: another error after sweep")],
        "show": (0, "different\n", ""),
    })
    now = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(
            repo=repo, quarantine_root=qdir, now=now,
        )

    assert result.success is False
    assert "pull --ff-only failed" in result.error
    # We DID move the file aside before discovering the deeper failure;
    # operators should see that in the audit trail.
    assert result.quarantined == [rel]


def test_pull_does_not_quarantine_when_marker_absent(tmp_path: Path):
    """A non-untracked-conflict failure (e.g. non-fast-forward) must NOT
    trigger any sweep — pulling that lever in the wrong cases would
    silently destroy local commits."""
    repo = tmp_path / "evolve-repo"
    repo.mkdir()
    qdir = tmp_path / "quarantine"

    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(repo=repo, quarantine_root=qdir)

    assert result.success is False
    assert result.deleted_identical == []
    assert result.quarantined == []
    assert not qdir.exists()


# ── tick(): notifier on first wedge ───────────────────────────────────────


def test_tick_notifies_on_first_wedge(tmp_path: Path):
    """A NEW puller-stuck issue must trigger a one-shot notification.
    This is the signal that closes the silent-failure gap that hid the
    publickey wedge for 5 days."""
    repo, _ = _make_repo_layout(tmp_path)
    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)

    calls: list[tuple[Path, str, Path]] = []
    def stub_notifier(issue_path, error, repo_arg):
        calls.append((issue_path, error, repo_arg))
        return True, ""

    now = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, now=now, notifier=stub_notifier)

    assert result.issue_was_new is True
    assert result.notified is True
    assert len(calls) == 1
    sent_issue, sent_error, sent_repo = calls[0]
    assert sent_issue == result.issue_path
    assert "Not possible to fast-forward" in sent_error
    assert sent_repo == repo


def test_tick_does_not_notify_on_recurrence(tmp_path: Path):
    """Recurrences within the dedup window must NOT re-fire the notifier.
    Otherwise the alerts channel floods every 15min until the wedge clears."""
    repo, _ = _make_repo_layout(tmp_path)
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)

    calls: list[tuple] = []
    def stub_notifier(issue_path, error, repo_arg):
        calls.append((issue_path, error))
        return True, ""

    t1 = dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 5, 17, 15, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git",
                      _make_git_runner({
                          "rev-parse": (0, "a" * 40, ""),
                          "pull": (1, "", "fatal: refusing"),
                      })), \
         patch.object(Path, "exists", fake_exists):
        r1 = repo_puller.tick(repo=repo, now=t1, notifier=stub_notifier)
    with patch.object(repo_puller, "_git",
                      _make_git_runner({
                          "rev-parse": (0, "a" * 40, ""),
                          "pull": (1, "", "fatal: refusing"),
                      })), \
         patch.object(Path, "exists", fake_exists):
        r2 = repo_puller.tick(repo=repo, now=t2, notifier=stub_notifier)

    assert r1.issue_was_new is True
    assert r2.issue_was_new is False
    assert r1.notified is True
    assert r2.notified is False
    assert len(calls) == 1


def test_tick_survives_notifier_exception(tmp_path: Path):
    """Notifier raising must not crash the daemon. The issue is still
    on disk; health surfaces it; the notifier failure is recorded."""
    repo, _ = _make_repo_layout(tmp_path)
    real_exists = Path.exists
    def fake_exists(self):
        return True if self == repo else real_exists(self)

    def boom(issue_path, error, repo_arg):
        raise RuntimeError("openclaw exploded")

    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: refusing"),
    })
    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(
            repo=repo,
            now=dt.datetime(2026, 5, 5, 17, 0, 0, tzinfo=dt.timezone.utc),
            notifier=boom,
        )

    assert result.pull.success is False
    assert result.issue_path is not None
    assert result.notified is False
    assert "openclaw exploded" in result.notify_error


def test_format_tick_for_log_surfaces_notification_state():
    """The daemon log line is the durable on-disk record of every tick.
    On a NEW wedge, it must indicate whether the alert went out — that's
    the operator's confirmation that the page reached the channel."""
    notified = repo_puller.TickResult(
        pull=repo_puller.PullResult(success=False, error="boom"),
        issue_path=Path("incidents/2026-05-05-001-repo-puller-wedged.md"),
        issue_was_new=True,
        notified=True,
    )
    out = repo_puller.format_tick_for_log(notified, quiet=True)
    assert "filed" in out
    assert "notified alerts channel" in out

    failed_notify = repo_puller.TickResult(
        pull=repo_puller.PullResult(success=False, error="boom"),
        issue_path=Path("incidents/2026-05-05-001-repo-puller-wedged.md"),
        issue_was_new=True,
        notified=False,
        notify_error="openclaw exit 1: chatId not configured",
    )
    out2 = repo_puller.format_tick_for_log(failed_notify, quiet=True)
    assert "notify FAILED" in out2
    assert "openclaw exit 1" in out2

    # Recurrences should not include any notify line — there was no
    # notify attempt, and a "didn't fire" message would be misleading.
    recurrence = repo_puller.TickResult(
        pull=repo_puller.PullResult(success=False, error="boom"),
        issue_path=Path("incidents/2026-05-05-001-repo-puller-wedged.md"),
        issue_was_new=False,
    )
    out3 = repo_puller.format_tick_for_log(recurrence, quiet=True)
    assert "notified" not in out3
    assert "notify FAILED" not in out3


# ── shared-repo config (multi-user perms hardening) ───────────────────────
#
# Without this, the daemon (running as evolve, umask 022) creates 755 dirs
# under .git/objects/ that lock out the human admin in the same staff
# group on the next manual `git pull`. The 2026-05-06 wedge — the puller
# was healthy but pod_admin_user got "insufficient permission for adding an
# object" — was the canonical case. Pin both the plist (umask 002) and
# the git config (core.sharedRepository=group) so a regression on either
# half surfaces immediately.


def test_render_plist_sets_umask_002():
    """The puller's bash command must set umask 002 before exec'ing the
    pull. Without it, fetch-unpacked .git/objects/<xx>/ dirs land at 755
    and lock out other staff-group writers."""
    plist = repo_puller.render_plist()
    assert "umask 002" in plist
    # Order matters: umask must be set BEFORE the sleep + exec so the
    # umask is in effect when the pull subprocess runs.
    umask_idx = plist.index("umask 002")
    exec_idx = plist.index("exec ")
    assert umask_idx < exec_idx, "umask must come before exec"


def test_ensure_shared_repo_config_skips_when_already_group(tmp_path: Path):
    """Idempotent: if the repo already has core.sharedRepository=group,
    don't shell out to set it again. Both the install path and any
    later normalize call should be cheap reads in the steady state."""
    calls: list[list[str]] = []
    def fake_git(repo, args):
        calls.append(args)
        if args[:2] == ["config", "core.sharedRepository"]:
            return 0, "group", ""
        raise AssertionError(f"unexpected git call: {args}")

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(Path, "exists", return_value=True):
        ok, msg = repo_puller.ensure_shared_repo_config(repo=tmp_path)

    assert ok is True
    assert "already" in msg
    # No subprocess.run should fire — only the read via _git.
    assert len(calls) == 1


def test_ensure_shared_repo_config_sets_when_unset(tmp_path: Path):
    """When the config isn't set (fresh install or pre-hardening repo),
    invoke `sudo -u evolve git config core.sharedRepository group` so
    the resulting config file is owned by the daemon user."""
    def fake_git(repo, args):
        if args[:2] == ["config", "core.sharedRepository"]:
            return 1, "", "key not found"
        raise AssertionError(f"unexpected git call: {args}")

    captured: dict = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller.subprocess, "run", fake_run), \
         patch.object(Path, "exists", return_value=True):
        ok, msg = repo_puller.ensure_shared_repo_config(repo=tmp_path)

    assert ok is True
    assert "set core.sharedRepository=group" in msg
    cmd = captured["cmd"]
    # Must run as evolve so the .git/config file is owned by the daemon.
    assert cmd[:3] == ["sudo", "-u", "evolve"]
    assert "config" in cmd
    assert "core.sharedRepository" in cmd
    assert "group" in cmd
    assert str(tmp_path) in " ".join(cmd)


def test_ensure_shared_repo_config_returns_failure_when_repo_missing(tmp_path: Path):
    """Defensive: a missing repo path must produce a clear error rather
    than a confusing git-config message. The install path treats this
    as a warning, not a fatal — but the operator sees the actual cause."""
    nonexistent = tmp_path / "does-not-exist"
    ok, msg = repo_puller.ensure_shared_repo_config(repo=nonexistent)
    assert ok is False
    assert "missing" in msg


def test_ensure_shared_repo_config_returns_failure_on_git_config_error(tmp_path: Path):
    """If `git config` itself fails (read-only fs, bad sudoers, etc.),
    surface the rc + stderr in the message so the install log records
    something actionable rather than a silent skip."""
    def fake_git(repo, args):
        return 1, "", "fatal: not a git repository"

    def fake_run(cmd, **kwargs):
        return type("R", (), {
            "returncode": 1, "stdout": "",
            "stderr": "sudo: pam_authenticate: Permission denied",
        })()

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller.subprocess, "run", fake_run), \
         patch.object(Path, "exists", return_value=True):
        ok, msg = repo_puller.ensure_shared_repo_config(repo=tmp_path)

    assert ok is False
    assert "git config failed" in msg
    assert "rc=1" in msg


# ── pin_filemode_off_if_nested() — the 2026-06-23 Linux freeze defense ─────────
#
# Defensive belt-and-suspenders: a recursive perms pass that flipped the deploy
# checkout's exec bits (100644→100755) made `git pull --ff-only` refuse under
# core.fileMode=true → fleet froze. Pinning core.fileMode=false on the NESTED
# (Linux) checkout makes git ignore exec-bit churn so the pull can't wedge. The
# macOS SIBLING checkout must be left untouched (the hard byte-identity
# invariant). Real git, no mocking — the config write is what we're pinning.


def _init_repo(path: Path, *, file_mode: str = "true") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo_puller._git(path, ["init", "-q"])
    repo_puller._git(path, ["config", "core.fileMode", file_mode])
    return path


def test_pin_filemode_sets_false_on_nested_linux_checkout(tmp_path: Path):
    shared = tmp_path / "shared"
    repo = _init_repo(shared / "repo")  # nested: repo is a child of shared
    ok, msg = repo_puller.pin_filemode_off_if_nested(repo, shared, sudo_evolve=False)
    assert ok and "set core.fileMode=false" in msg
    assert repo_puller._git(repo, ["config", "core.fileMode"])[1].strip() == "false"


def test_pin_filemode_idempotent_when_already_false(tmp_path: Path):
    shared = tmp_path / "shared"
    repo = _init_repo(shared / "repo", file_mode="false")
    ok, msg = repo_puller.pin_filemode_off_if_nested(repo, shared, sudo_evolve=False)
    assert ok and "already false" in msg


def test_pin_filemode_skips_macos_sibling_checkout_untouched(tmp_path: Path):
    # repo is a SIBLING of shared (macOS shape) → must NOT be touched, so a mac
    # mini's git behavior stays byte-identical.
    shared = tmp_path / "shared"
    shared.mkdir()
    repo = _init_repo(tmp_path / "shared-repo", file_mode="true")
    ok, msg = repo_puller.pin_filemode_off_if_nested(repo, shared, sudo_evolve=False)
    assert ok and "not nested" in msg
    assert repo_puller._git(repo, ["config", "core.fileMode"])[1].strip() == "true"


def test_pin_filemode_skips_non_git_checkout(tmp_path: Path):
    shared = tmp_path / "shared"
    plain = shared / "repo"
    plain.mkdir(parents=True)  # nested but no .git (tarball-staged shape)
    ok, msg = repo_puller.pin_filemode_off_if_nested(plain, shared, sudo_evolve=False)
    assert ok and "not a git working tree" in msg


def test_pull_pins_filemode_off_before_ff_only_on_nested_checkout(tmp_path: Path):
    # pull() must pin core.fileMode=false (a `config core.fileMode false` write)
    # BEFORE it runs `git pull --ff-only`, when the repo is nested under
    # shared_dir. Assert the ordering via the fake-git call log.
    shared = tmp_path / "shared"
    repo = shared / "repo"
    repo.mkdir(parents=True)
    sha = "a" * 40  # no-op pull (before == after) keeps the fake minimal
    seen: list[list[str]] = []

    def fake(repo_arg, args):
        seen.append(args)
        head = args[0] if args else ""
        if head == "rev-parse":
            return (0, sha, "")
        if head == "config":
            return (0, "true", "") if args == ["config", "core.fileMode"] else (0, "", "")
        if head == "pull":
            return (0, "Already up to date.", "")
        raise AssertionError(f"unexpected git call: {args}")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=repo, shared_dir=shared)

    assert result.success is True
    set_idx = seen.index(["config", "core.fileMode", "false"])
    pull_idx = next(i for i, a in enumerate(seen) if a and a[0] == "pull")
    assert set_idx < pull_idx, "core.fileMode=false must be set BEFORE the ff-only pull"


def test_pull_does_not_pin_filemode_on_sibling_layout(tmp_path: Path):
    # repo NOT under shared_dir (macOS sibling) → pull() must not touch
    # core.fileMode at all (byte-identical macOS behavior).
    repo = tmp_path / "evolve-repo"
    shared = tmp_path / "evolve"
    repo.mkdir()
    shared.mkdir()
    sha = "a" * 40  # no-op pull (before == after)
    seen: list[list[str]] = []

    def fake(repo_arg, args):
        seen.append(args)
        head = args[0] if args else ""
        if head == "rev-parse":
            return (0, sha, "")
        if head == "pull":
            return (0, "Already up to date.", "")
        if head == "config":
            raise AssertionError("pull touched core.fileMode on the sibling layout")
        raise AssertionError(f"unexpected git call: {args}")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=repo, shared_dir=shared)
    assert result.success is True
    assert not any(a[:1] == ["config"] for a in seen)


# ── daemons_for_paths() — path → daemon mapping ───────────────────────────
#
# The mapping is the load-bearing piece: a wrong rule either silently
# leaves a daemon running pre-pull code (PR #867 shape) or churns a
# daemon needlessly on every unrelated pull. Pin both shapes.


@pytest.mark.parametrize("paths,expected_daemons,expected_warning_count", [
    # Empty diff → nothing to restart.
    ([], set(), 0),
    # Pure docs / config → nothing to restart.
    (["docs/CHANGELOG.md", "README.md", ".github/workflows/ci.yml"],
     set(), 0),
    # Admin server change → admin-ui only.
    (["packages/admin/evolve_admin/server.py"],
     {"ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"}, 0),
    # Admin web bundle (HTML/JS) → admin-ui only. Same rule because the
    # operator can't tell HTML changes apart from server.py changes.
    (["packages/admin/evolve_admin/web/index.html"],
     {"ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"}, 0),
    (["packages/admin/evolve_admin/web/style.css"],
     {"ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"}, 0),
    # Analyzer change → heal/audit/verify, NOT admin-ui.
    (["packages/analyzer/heal/runner.py"], {
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.verify",
    }, 0),
    # Both admin and analyzer in the same diff → union.
    (["packages/admin/evolve_admin/server.py",
      "packages/analyzer/heal/runner.py"], {
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.mcp-bridge",
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.verify",
    }, 0),
    # Plugin-only change → nothing in this mapping (handled separately
    # by the plugin-rebuild path; openclaw gateways aren't ai.evolve.* daemons).
    (["packages/plugin/src/tools/DeferTool.ts"], set(), 0),
])
def test_daemons_for_paths_basic_mappings(
    paths, expected_daemons, expected_warning_count,
):
    """Exhaustive parametrize over the mapping rules. A new rule should
    show up here as a new row."""
    daemons, warnings = repo_puller.daemons_for_paths(paths)
    assert daemons == expected_daemons
    assert len(warnings) == expected_warning_count


def test_daemons_for_paths_skips_self_with_warning():
    """If repo_puller.py itself changed, the puller must NOT restart
    its own daemon mid-tick (racy: launchd may kill the process before
    the kickstart syscall returns). Surface a warning so the operator
    knows a manual kickstart is required to pick up the new code."""
    daemons, warnings = repo_puller.daemons_for_paths([
        "packages/admin/evolve_admin/repo_puller.py",
    ])
    # repo_puller.py IS under packages/admin/, so the rule would match
    # admin-ui — but the self-skip carve-out runs first and removes it.
    assert daemons == set()
    assert len(warnings) == 1
    w = warnings[0]
    assert "repo_puller.py" in w
    assert "manual" in w.lower()
    assert "ai.evolve.evolve.repo-puller" in w


def test_daemons_for_paths_self_skip_does_not_block_other_rules():
    """If repo_puller.py changes alongside other admin code, the OTHER
    code still triggers admin-ui restart. The carve-out applies only to
    the puller's own self-restart, not to all admin-ui restarts in the
    same diff."""
    daemons, warnings = repo_puller.daemons_for_paths([
        "packages/admin/evolve_admin/repo_puller.py",
        "packages/admin/evolve_admin/server.py",
    ])
    assert daemons == {"ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"}
    assert len(warnings) == 1   # the puller-self warning


def test_daemons_for_paths_dedupes_when_multiple_files_match_same_rule():
    """Five files under packages/admin/ should still produce just one
    admin-ui label, not five. Defends against accidental list-vs-set."""
    daemons, _ = repo_puller.daemons_for_paths([
        "packages/admin/evolve_admin/server.py",
        "packages/admin/evolve_admin/cli.py",
        "packages/admin/evolve_admin/health.py",
        "packages/admin/evolve_admin/web/index.html",
        "packages/admin/evolve_admin/web/style.css",
    ])
    assert daemons == {"ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"}


# ── _auto_restart_enabled() — kill switch ─────────────────────────────────


@pytest.mark.parametrize("env_value,expected", [
    ("1", True),
    ("true", True),
    ("yes", True),
    ("on", True),
    ("anything-non-falsy", True),
    ("0", False),
    ("false", False),
    ("False", False),
    ("no", False),
    ("off", False),
    ("OFF", False),
    ("", False),
])
def test_auto_restart_enabled_respects_env_values(monkeypatch, env_value, expected):
    """The kill switch must accept the obvious truthy/falsy spellings.
    A typo (``EVOLVE_PULLER_AUTO_RESTART=fals``) should NOT silently
    enable auto-restart — that would defeat the point of the switch."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, env_value)
    assert repo_puller._auto_restart_enabled() is expected


def test_auto_restart_enabled_defaults_on(monkeypatch):
    """Default ON: if the env var is unset, the puller proceeds with
    auto-restart. Forgetting to set it must NOT silently disable the
    feature in production (where the whole point is restart-by-default)."""
    monkeypatch.delenv(repo_puller.AUTO_RESTART_ENV, raising=False)
    assert repo_puller._auto_restart_enabled() is True


# ── pull() integration — kickstart wiring ─────────────────────────────────


def test_pull_kickstarts_admin_ui_when_admin_path_pulled(tmp_path: Path, monkeypatch):
    """End-to-end: a pull that advances HEAD with `packages/admin/...`
    in the diff must invoke the kickstart_fn with the admin-ui label.
    This is the load-bearing wiring that closes the PR #867 gap."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py\n"
                    "docs/note.md", ""),
    })
    kicks: list[str] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True
    assert kicks == ["ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"]
    assert result.restarted_daemons == ["ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"]
    assert result.restart_errors == {}
    assert result.restart_skipped_disabled is False


def test_pull_kickstarts_analyzer_daemons_in_sorted_order(tmp_path: Path, monkeypatch):
    """Pulling analyzer code restarts heal/audit/verify. Pin sorted
    order so log lines stay deterministic across runs (operators
    grep them; non-deterministic order makes that brittle)."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/analyzer/heal/runner.py", ""),
    })
    kicks: list[str] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True
    assert kicks == [
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.verify",
    ]
    assert result.restarted_daemons == kicks


def test_pull_records_kickstart_failures_without_failing_pull(tmp_path: Path, monkeypatch):
    """A kickstart failure (held process, bad sudoers, etc.) must NOT
    fail the overall pull — HEAD has already advanced. The error
    surfaces in restart_errors + the puller log so the operator can
    re-run manually. Best-effort, by design."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py", ""),
    })
    def fake_kick(label: str) -> tuple[bool, str]:
        return False, "rc=1: process held"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True   # pull still succeeded
    assert result.restarted_daemons == []
    assert result.restart_errors == {
        "ai.evolve.evolve.admin-ui": "rc=1: process held",
        "ai.evolve.evolve.mcp-bridge": "rc=1: process held",
    }
    assert any("FAIL restart" in s for s in result.steps)


def test_pull_kickstart_fn_exception_does_not_crash_pull(tmp_path: Path, monkeypatch):
    """An unexpected exception from kickstart_fn (timeout subclass,
    bad CalledProcessError, anything) must be caught — the daemon's
    correctness is more important than the auto-restart side effect."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py", ""),
    })
    def boom(label: str) -> tuple[bool, str]:
        raise RuntimeError("launchctl exploded")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=boom)

    assert result.success is True
    assert result.restarted_daemons == []
    assert "ai.evolve.evolve.admin-ui" in result.restart_errors
    assert "launchctl exploded" in result.restart_errors["ai.evolve.evolve.admin-ui"]


def test_pull_skips_kickstart_when_killswitch_disabled(tmp_path: Path, monkeypatch):
    """``EVOLVE_PULLER_AUTO_RESTART=0`` is the production kill switch.
    With it set, daemons that would otherwise restart get logged as
    "would have restarted" but kickstart_fn is NOT called. This is the
    30-second-mitigation lever for a misbehaving auto-restart."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "0")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py", ""),
    })
    kicks: list[str] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True
    assert kicks == [], "kickstart_fn must not run when killswitch is 0"
    assert result.restart_skipped_disabled is True
    assert result.restarted_daemons == []
    assert any("auto-restart disabled" in s for s in result.steps)


def test_pull_skips_kickstart_when_only_repo_puller_changed(tmp_path: Path, monkeypatch):
    """If the pulled diff touches ONLY repo_puller.py, no daemon
    restart fires (the puller refuses to restart itself; admin-ui has
    no separate trigger). The warning is recorded so the operator
    knows a manual kickstart is required."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/repo_puller.py", ""),
    })
    kicks: list[str] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True
    assert kicks == []
    assert result.restarted_daemons == []
    assert len(result.restart_warnings) == 1
    assert "repo_puller.py" in result.restart_warnings[0]


def test_pull_no_op_does_not_kickstart(tmp_path: Path, monkeypatch):
    """An already-up-to-date pull has no diff to inspect, no daemons
    to restart, no kickstart_fn call. Pin this to defend against a
    regression where we'd run the diff probe even on no-op pulls and
    inadvertently restart daemons because the diff command failed
    (returning [] which then matches every prefix... wait, actually
    that's safe by construction; pin it anyway because regressions
    of this shape have surfaced before)."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    sha = "a" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
    })
    kicks: list[str] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, kickstart_fn=fake_kick)

    assert result.success is True
    assert kicks == []
    assert result.restarted_daemons == []


def test_pull_kickstart_runs_after_plugin_rebuild(tmp_path: Path, monkeypatch):
    """When the diff touches BOTH plugin code AND admin code, the plugin
    rebuild and the admin-ui kickstart must both run. Pin the order:
    plugin rebuild first (the staged dist must be in place before any
    consumer reloads), then daemon kickstarts. Without this ordering,
    a kicked admin-ui could reload mid-rebuild and pick up half-staged
    plugin state."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    sequence: list[str] = []
    rev_parse_calls = [0]
    def tracking_git(repo: Path, args: list[str]) -> tuple[int, str, str]:
        sequence.append(args[0])
        if args[0] == "rev-parse":
            rev_parse_calls[0] += 1
            return (0, before if rev_parse_calls[0] == 1 else after, "")
        if args[0] == "pull":
            return (0, "Updating ...", "")
        if args[0] == "log":
            return (0, "abc commit\n", "")
        if args[0] == "diff":
            return (0, "packages/admin/evolve_admin/server.py\n"
                       "packages/plugin/src/tools/DeferTool.ts", "")
        if args[0] == "update-index":
            return (0, "", "")
        raise AssertionError(f"unexpected: {args}")

    rebuild_at: list[int] = []
    def fake_rebuild() -> tuple[bool, str]:
        rebuild_at.append(len(sequence))
        return True, "rebuilt + staged"

    kick_at: list[tuple[int, str]] = []
    def fake_kick(label: str) -> tuple[bool, str]:
        kick_at.append((len(sequence), label))
        return True, "ok"

    with patch.object(repo_puller, "_git", tracking_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=fake_kick,
        )

    assert result.success is True
    assert result.plugin_rebuilt is True
    assert result.restarted_daemons == ["ai.evolve.evolve.admin-ui", "ai.evolve.evolve.mcp-bridge"]
    assert len(rebuild_at) == 1
    assert len(kick_at) == 2  # admin-ui + mcp-bridge both kicked
    # Plugin rebuild ran before the kickstart — pin via the captured
    # sequence index.
    assert rebuild_at[0] < kick_at[0][0], (
        f"plugin rebuild must precede daemon kickstart; "
        f"rebuild_at={rebuild_at}, kick_at={kick_at}"
    )


def test_pull_restart_failure_does_not_obscure_plugin_rebuild_success(
    tmp_path: Path, monkeypatch,
):
    """The two best-effort post-pull steps (plugin rebuild + daemon
    kickstart) must report INDEPENDENT outcomes. A kickstart failure
    must not roll back or mask a successful plugin rebuild — they're
    different concerns and both surface in the puller log."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/admin/evolve_admin/server.py\n"
                    "packages/plugin/src/tools/DeferTool.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild() -> tuple[bool, str]:
        return True, "rebuilt + staged"
    def failing_kick(label: str) -> tuple[bool, str]:
        return False, "rc=1: held"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=failing_kick,
        )

    assert result.success is True
    assert result.plugin_rebuilt is True
    assert result.plugin_rebuild_error == ""
    assert result.restarted_daemons == []
    assert "ai.evolve.evolve.admin-ui" in result.restart_errors


# ── format_for_log surfaces restart info ──────────────────────────────────


def test_format_for_log_surfaces_restarted_daemons():
    """Operators reading the puller log need to see WHICH daemons got
    restarted on a given tick — that's how they correlate "feature X
    appeared" with "the relevant daemon picked up the new code"."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=2,
        restarted_daemons=["ai.evolve.evolve.admin-ui"],
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "restarted daemons" in out
    assert "ai.evolve.evolve.admin-ui" in out


def test_format_for_log_surfaces_restart_errors():
    """A kickstart failure must appear in the log as a WARN line so
    the operator's grep catches it. Silent failures here would
    reproduce the PR #867 gap (code on disk, daemon stale)."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        restart_errors={"ai.evolve.evolve.admin-ui": "rc=1: held"},
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "WARN restart" in out
    assert "ai.evolve.evolve.admin-ui" in out
    assert "held" in out


def test_format_for_log_surfaces_killswitch_disabled():
    """When the kill switch is on, the operator running ``--quiet``
    should still see that auto-restart was skipped — otherwise the
    silent-no-op would hide the production mitigation state."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        restart_skipped_disabled=True,
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "auto-restart disabled" in out
    assert repo_puller.AUTO_RESTART_ENV in out


def test_format_for_log_surfaces_self_skip_warning():
    """The repo_puller.py self-skip is the only way an operator learns
    they need to manually kickstart the puller after an update to the
    puller itself. Pin that the warning makes it into the log."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        restart_warnings=[
            "packages/admin/evolve_admin/repo_puller.py changed; "
            "skipping self-restart "
            "(manual `sudo /bin/launchctl kickstart -k "
            "system/ai.evolve.evolve.repo-puller` required, "
            "or wait for the next 15-min cron tick)",
        ],
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "repo_puller.py changed" in out
    assert "manual" in out


# ── _kickstart_daemon — Scheduler-seam wiring ─────────────────────────────
#
# _kickstart_daemon restarts daemons through the process-wide get_scheduler()
# seam (NOT a module-global LaunchdScheduler), so the Linux platform gate's
# set_scheduler(SystemdScheduler()) injection is honored. These tests inject a
# fake-runner LaunchdScheduler via runtime.set_scheduler so they exercise the
# real macOS adapter argv (sudo /bin/launchctl kickstart -k system/<label>)
# without ever reaching a live launchctl. The _seam_scheduler fixture restores
# the process-wide singleton afterwards via set_scheduler(None) — without that
# teardown the fake leaks into every later test that calls get_scheduler().


@pytest.fixture
def _seam_scheduler():
    """Inject a fake-runner LaunchdScheduler into the process-wide seam and
    yield a factory that records (the kickstart argv, the per-call ``timeout``
    passed to ``restart``). Resets the singleton on teardown."""
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    captured: dict = {}

    def install(runner):
        # Spy on restart() to pin the per-call timeout the puller threads in
        # (15s — load-bearing; ~7 sequential restarts must stay bounded, and
        # get_scheduler()'s default is 30s). A future edit dropping the bound
        # is caught here.
        sched = LaunchdScheduler(runner=runner)
        real_restart = sched.restart

        def spy_restart(label, *, timeout=None):
            captured["restart_timeout"] = timeout
            return real_restart(label, timeout=timeout)

        sched.restart = spy_restart  # type: ignore[method-assign]
        set_scheduler(sched)
        return captured

    try:
        yield install
    finally:
        set_scheduler(None)


def test_kickstart_daemon_uses_sudo_launchctl(_seam_scheduler):
    """The kickstart must reach the seam's macOS adapter as
    ``sudo /bin/launchctl kickstart -k system/<label>`` and pass the
    load-bearing 15s per-call timeout to ``restart``. Anything else either
    bypasses sudoers (won't have permission) or won't actually restart the
    daemon — and dropping the timeout would let ~7 sequential restarts run
    unbounded."""
    captured: dict = {}

    def fake_runner(argv):
        captured["argv"] = argv
        return 0, "", ""  # (rc, stdout, stderr)

    recorded = _seam_scheduler(fake_runner)

    ok, info = repo_puller._kickstart_daemon("ai.evolve.evolve.admin-ui")

    assert ok is True
    assert info == "ok"
    # The fake runner received the real macOS launchd argv.
    assert captured["argv"] == [
        "sudo", "/bin/launchctl", "kickstart", "-k",
        "system/ai.evolve.evolve.admin-ui",
    ]
    # restart() got the 15s bound (pins the load-bearing timeout).
    assert recorded["restart_timeout"] == 15.0


def test_kickstart_daemon_returns_failure_with_stderr(_seam_scheduler):
    """A non-zero exit must yield ok=False with stderr in the info
    string so the operator sees the actual failure cause — and never raise."""
    def fake_runner(argv):
        return (
            1,
            "",
            "Could not find service ai.evolve.evolve.admin-ui in domain for system",
        )

    _seam_scheduler(fake_runner)

    ok, info = repo_puller._kickstart_daemon("ai.evolve.evolve.admin-ui")

    assert ok is False
    # Post Scheduler-seam migration the rc itself is no longer embedded
    # (Scheduler.restart reports combined output) — the launchctl
    # diagnostic text is the load-bearing part.
    assert "Could not find service" in info


def test_kickstart_daemon_handles_timeout(_seam_scheduler):
    """A held launchctl call must time out cleanly; the puller's tick should
    not hang waiting for it, and the timeout must surface as (False, msg) —
    never a raised exception (the never-raise contract).

    The seam's default runner (``_subprocess_runner``) catches a
    ``TimeoutExpired`` and reports it as ``(1, "", str(e))``; the fake runner
    stands in for that runner, so it returns the SAME swallowed shape rather
    than raising. This faithfully exercises the (False, msg) contract the
    puller depends on (a raise here would crash the 15-min tick)."""
    timeout_msg = str(subprocess.TimeoutExpired(cmd="sudo /bin/launchctl", timeout=15))

    def fake_runner(argv):
        return 1, "", timeout_msg

    _seam_scheduler(fake_runner)

    ok, info = repo_puller._kickstart_daemon("ai.evolve.evolve.admin-ui")

    assert ok is False
    assert "timed out" in info.lower()


# ── render_plist exposes kill-switch env var ──────────────────────────────


def test_render_plist_declares_auto_restart_env_var():
    """The kill switch needs to be discoverable: an operator opening the
    plist should see EVOLVE_PULLER_AUTO_RESTART=1 in the env block.
    Without this, the only place the variable is documented is in the
    Python source — operators reach for the plist first when mitigating
    a daemon-restart misbehavior."""
    plist = repo_puller.render_plist()
    assert repo_puller.AUTO_RESTART_ENV in plist
    # Pin the default — flipping it ON-by-default is the whole point.
    auto_idx = plist.index(repo_puller.AUTO_RESTART_ENV)
    after_key = plist[auto_idx:]
    assert "<string>1</string>" in after_key.split("</key>", 1)[1].split("<key", 1)[0]


# ── install_launchd — self-bootout guard ──────────────────────────────────


def _spy_subprocess_run():
    """Build a recording subprocess.run replacement.

    Returns (spy, calls). ``calls`` is a list of (argv_list, returncode)
    tuples — every subprocess.run invocation appends one entry."""
    calls: list[tuple[list[str], int]] = []

    class _CP:
        def __init__(self, argv):
            self.args = argv
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def _run(argv, *args, **kwargs):
        cp = _CP(list(argv))
        calls.append((cp.args, cp.returncode))
        return cp

    return _run, calls


def _argv_contains_bootout_self(argv: list[str]) -> bool:
    return (
        "launchctl" in " ".join(argv)
        and "bootout" in argv
        and f"system/{repo_puller.REPO_PULLER_LABEL}" in argv
    )


def _argv_contains_bootstrap_self(argv: list[str]) -> bool:
    return (
        "launchctl" in " ".join(argv)
        and "bootstrap" in argv
        and repo_puller.REPO_PULLER_PLIST in argv
    )


def test_install_launchd_skips_dance_when_plist_content_unchanged(monkeypatch, tmp_path):
    """The auto-install hook (PR #920) re-runs install_evolve_infra_jobs on
    every deploy.py-touching pull, but the puller's own plist content
    rarely changes. When it matches, install_launchd must not bootout +
    bootstrap — every cycle through that dance is a SIGTERM risk for the
    puller process itself (witnessed 2026-05-10, PR #953)."""
    fake_plist = tmp_path / "ai.evolve.evolve.repo-puller.plist"
    fake_plist.write_text(repo_puller.render_plist())
    monkeypatch.setattr(repo_puller, "REPO_PULLER_PLIST", str(fake_plist))

    # Pretend the deploy-key + shared-repo-config steps are no-ops so we
    # only assert on what install_launchd itself drove.
    monkeypatch.setattr(repo_puller, "ensure_shared_repo_config", lambda: (True, "ok"))

    class _DK:
        success = True
        steps: list[str] = []
        auth_test_ok = True
        error = ""
    monkeypatch.setattr(repo_puller, "ensure_deploy_key", lambda: _DK())
    monkeypatch.setattr(repo_puller, "format_deploy_key_instructions",
                        lambda dk, repo_url=None: "")

    spy, calls = _spy_subprocess_run()
    monkeypatch.setattr(repo_puller.subprocess, "run", spy)

    logs: list[str] = []
    ok = repo_puller.install_launchd(result_logger=logs.append)
    assert ok is True

    # No subprocess call for bootout/bootstrap of the puller.
    assert not any(_argv_contains_bootout_self(argv) for argv, _ in calls), (
        f"unexpected self-bootout when plist content unchanged: {calls}"
    )
    assert not any(_argv_contains_bootstrap_self(argv) for argv, _ in calls), (
        f"unexpected self-bootstrap when plist content unchanged: {calls}"
    )
    # Operator-visible breadcrumb that idempotency kicked in.
    assert any("already up to date" in line for line in logs), logs


def test_install_launchd_skips_self_bootout_when_running_inside_puller(monkeypatch, tmp_path):
    """When install_launchd is invoked from within the puller process
    (env var PULLER_PROCESS_ENV is set) and the plist content genuinely
    changed, the new content must land on disk but the bootout +
    bootstrap dance must be skipped. A self-bootout would SIGTERM the
    running puller before the bootstrap could re-register the daemon —
    that's exactly the failure mode this guard prevents."""
    # Set up a stale plist that differs from render_plist() so the
    # idempotency fast-path does NOT apply.
    fake_plist = tmp_path / "ai.evolve.evolve.repo-puller.plist"
    fake_plist.write_text("<plist>stale</plist>")
    monkeypatch.setattr(repo_puller, "REPO_PULLER_PLIST", str(fake_plist))

    # Mark ourselves as the puller process.
    monkeypatch.setenv(repo_puller.PULLER_PROCESS_ENV, "12345")

    monkeypatch.setattr(repo_puller, "ensure_shared_repo_config", lambda: (True, "ok"))

    class _DK:
        success = True
        steps: list[str] = []
        auth_test_ok = True
        error = ""
    monkeypatch.setattr(repo_puller, "ensure_deploy_key", lambda: _DK())
    monkeypatch.setattr(repo_puller, "format_deploy_key_instructions",
                        lambda dk, repo_url=None: "")

    spy, calls = _spy_subprocess_run()
    monkeypatch.setattr(repo_puller.subprocess, "run", spy)

    logs: list[str] = []
    ok = repo_puller.install_launchd(result_logger=logs.append)
    assert ok is True

    # /bin/cp must run (the new plist still needs to land on disk so the
    # next reboot picks it up); bootout/bootstrap of the puller's own
    # label must NOT.
    assert any("/bin/cp" in " ".join(argv) for argv, _ in calls), (
        f"plist must still be written to disk: {calls}"
    )
    assert not any(_argv_contains_bootout_self(argv) for argv, _ in calls), (
        f"unexpected self-bootout from inside the puller: {calls}"
    )
    assert not any(_argv_contains_bootstrap_self(argv) for argv, _ in calls), (
        f"unexpected self-bootstrap from inside the puller: {calls}"
    )
    # Operator gets a clear breadcrumb about the deferred reload.
    assert any("self-bootstrap skipped" in line for line in logs), logs
    assert any("kickstart -k" in line for line in logs), logs


def test_install_launchd_bootstraps_when_outside_puller_and_content_changed(
    monkeypatch, tmp_path
):
    """The normal path — invoked from a shell (not inside the puller) and
    plist content differs — must perform bootout + bootstrap. Without
    this, the new content would only take effect at the next reboot."""
    fake_plist = tmp_path / "ai.evolve.evolve.repo-puller.plist"
    fake_plist.write_text("<plist>stale</plist>")
    monkeypatch.setattr(repo_puller, "REPO_PULLER_PLIST", str(fake_plist))

    # Make sure the puller marker is NOT set.
    monkeypatch.delenv(repo_puller.PULLER_PROCESS_ENV, raising=False)

    monkeypatch.setattr(repo_puller, "ensure_shared_repo_config", lambda: (True, "ok"))

    class _DK:
        success = True
        steps: list[str] = []
        auth_test_ok = True
        error = ""
    monkeypatch.setattr(repo_puller, "ensure_deploy_key", lambda: _DK())
    monkeypatch.setattr(repo_puller, "format_deploy_key_instructions",
                        lambda dk, repo_url=None: "")

    spy, calls = _spy_subprocess_run()
    monkeypatch.setattr(repo_puller.subprocess, "run", spy)

    # Point the Scheduler seam at tmp_path so install() compares/boots
    # the SAME plist path the test redirected REPO_PULLER_PLIST to —
    # never the real /Library/LaunchDaemons (whose content this unit
    # test must not depend on).
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler
    set_scheduler(LaunchdScheduler(plist_dir=tmp_path))
    try:
        ok = repo_puller.install_launchd(result_logger=lambda _msg: None)
    finally:
        set_scheduler(None)
    assert ok is True
    assert any(_argv_contains_bootout_self(argv) for argv, _ in calls), calls
    assert any(_argv_contains_bootstrap_self(argv) for argv, _ in calls), calls


# ── bot gateway kickstart after plugin rebuild ────────────────────────────
#
# Background (PR #1639, cascade Phase 1 telemetry): the repo-puller already
# rebuilds the plugin dist on packages/plugin/ changes AND restarts the
# ai.evolve.* admin daemons via daemons_for_paths, but it never touched
# the ai.openclaw.<bot>-gateway daemons. Those are the long-running node
# processes that load the plugin once at startup, so plugin changes had
# zero observable effect on bot behavior until an operator manually
# kickstarted them. These tests pin the post-rebuild kickstart hook that
# closes the gap.


def test_discover_bot_gateways_returns_sorted_labels(tmp_path: Path):
    """Plist scan returns every ai.openclaw.*-gateway.plist as a sorted
    label list. Includes ai.openclaw.evolve-gateway — it loads the plugin
    too. Excludes anything that doesn't match the gateway suffix (e.g.
    legacy ai.openclaw.team_bot_a-healthcheck.plist must not be picked up)."""
    (tmp_path / "ai.openclaw.team_bot_a-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.admin_bot-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.evolve-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.team_bot_a-healthcheck.plist").write_text("x")
    (tmp_path / "ai.evolve.evolve.admin-ui.plist").write_text("x")
    labels = repo_puller._discover_bot_gateways(launchd_dir=tmp_path)
    assert labels == [
        "ai.openclaw.admin_bot-gateway",
        "ai.openclaw.evolve-gateway",
        "ai.openclaw.team_bot_a-gateway",
    ]


def test_discover_bot_gateways_returns_empty_when_dir_missing(tmp_path: Path):
    """A missing launchd dir (e.g. fresh container, mock setup) must
    return [] rather than raising — the puller treats it as 'nothing to
    restart' and the tick proceeds cleanly."""
    nonexistent = tmp_path / "no-such-dir"
    assert repo_puller._discover_bot_gateways(launchd_dir=nonexistent) == []


def test_short_bot_name_strips_prefix_and_suffix():
    """Log lines show `team_bot_a` not `ai.openclaw.team_bot_a-gateway` — operators
    scanning logs want the bot name, not the launchd label."""
    assert repo_puller._short_bot_name("ai.openclaw.team_bot_a-gateway") == "team_bot_a"
    assert repo_puller._short_bot_name("ai.openclaw.evolve-gateway") == "evolve"


def test_pull_kickstarts_bot_gateways_after_plugin_rebuild(
    tmp_path: Path, monkeypatch
):
    """End-to-end: when the pulled diff touches packages/plugin/, the
    rebuild runs successfully, and discovery returns 3 gateways, the
    puller kickstarts all 3. This is the load-bearing wiring that closes
    the PR #1639 gap — without it, plugin changes never reach the
    running bot gateways until an operator intervenes."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    def fake_discover():
        return [
            "ai.openclaw.evolve-gateway",
            "ai.openclaw.team_bot_a-gateway",
            "ai.openclaw.admin_bot-gateway",
        ]
    kicks: list[str] = []
    def fake_kick(label):
        kicks.append(label)
        return True, "ok"
    sleeps: list[float] = []
    def fake_sleep(s):
        sleeps.append(s)

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
            gateway_sleep_fn=fake_sleep,
        )

    assert result.success is True
    assert result.plugin_rebuilt is True
    assert result.bot_gateways_restarted == [
        "ai.openclaw.evolve-gateway",
        "ai.openclaw.team_bot_a-gateway",
        "ai.openclaw.admin_bot-gateway",
    ]
    assert result.bot_gateway_restart_errors == {}
    assert kicks == [
        "ai.openclaw.evolve-gateway",
        "ai.openclaw.team_bot_a-gateway",
        "ai.openclaw.admin_bot-gateway",
    ]
    # Stagger fires between each pair (n-1 sleeps for n kickstarts).
    assert len(sleeps) == 2
    assert all(s == repo_puller.DEFAULT_GATEWAY_KICKSTART_STAGGER_SECONDS
               for s in sleeps)


def test_pull_skips_gateway_kickstart_when_no_plugin_change(
    tmp_path: Path, monkeypatch
):
    """Diff without `packages/plugin/` paths → no rebuild → no gateway
    kickstart. Pinning this defends against a regression where we'd
    inadvertently restart every bot on every pull (which would interrupt
    in-flight sessions and silently make pulls user-visible)."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "docs/CHANGELOG.md", ""),
    })
    discovery_calls: list[bool] = []
    def fake_discover():
        discovery_calls.append(True)
        return ["ai.openclaw.team_bot_a-gateway"]
    kicks: list[str] = []
    def fake_kick(label):
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
        )

    assert result.success is True
    assert result.plugin_rebuilt is False
    # Discovery was never even invoked.
    assert discovery_calls == []
    assert kicks == []
    assert result.bot_gateways_restarted == []


def test_pull_skips_gateway_kickstart_when_plugin_rebuild_fails(
    tmp_path: Path, monkeypatch
):
    """If the rebuild fails, the staged dist is unchanged — restarting
    gateways would just have them reload the same stale code. Pin that
    we don't restart in this case (purely additive cost with zero
    benefit; gateways stay up and operator sees the rebuild failure)."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (1, "", ""),
    })
    def failing_rebuild():
        return False, "tsc failed: TS5033"
    discovery_calls: list[bool] = []
    def fake_discover():
        discovery_calls.append(True)
        return ["ai.openclaw.team_bot_a-gateway"]
    kicks: list[str] = []
    def fake_kick(label):
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=failing_rebuild,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
        )

    assert result.success is True
    assert result.plugin_rebuilt is False
    assert "tsc failed" in result.plugin_rebuild_error
    assert discovery_calls == []
    assert kicks == []
    assert result.bot_gateways_restarted == []


def test_pull_records_gateway_kickstart_failures_without_failing_pull(
    tmp_path: Path, monkeypatch
):
    """A kickstart failure on one gateway must not abort the rest, must
    not fail the overall pull. The error surfaces in the result so the
    operator can re-run manually for the affected bot."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    def fake_discover():
        return ["ai.openclaw.team_bot_a-gateway", "ai.openclaw.admin_bot-gateway"]
    def fake_kick(label):
        if label == "ai.openclaw.admin_bot-gateway":
            return False, "rc=1: process held"
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
            gateway_sleep_fn=lambda _: None,
        )

    assert result.success is True
    assert result.bot_gateways_restarted == ["ai.openclaw.team_bot_a-gateway"]
    assert result.bot_gateway_restart_errors == {
        "ai.openclaw.admin_bot-gateway": "rc=1: process held",
    }


def test_pull_gateway_kickstart_exception_does_not_crash_pull(
    tmp_path: Path, monkeypatch
):
    """An unexpected exception from kickstart_fn (subprocess.TimeoutExpired,
    OSError, anything) must be caught — the daemon's correctness is more
    important than the auto-restart side effect."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    def fake_discover():
        return ["ai.openclaw.team_bot_a-gateway"]
    def boom(label):
        raise RuntimeError("launchctl exploded")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=boom,
            gateway_discovery_fn=fake_discover,
            gateway_sleep_fn=lambda _: None,
        )

    assert result.success is True
    assert result.bot_gateways_restarted == []
    assert "RuntimeError" in result.bot_gateway_restart_errors[
        "ai.openclaw.team_bot_a-gateway"
    ]


def test_pull_gateway_discovery_failure_does_not_fail_pull(
    tmp_path: Path, monkeypatch
):
    """If discovery itself raises (permissions, glob blow-up, anything),
    record the error but let the pull succeed — HEAD has already
    advanced. Operator sees the failure in the log + can intervene."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    def boom_discover():
        raise PermissionError("cannot stat /Library/LaunchDaemons")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            gateway_discovery_fn=boom_discover,
        )

    assert result.success is True
    assert "PermissionError" in result.bot_gateway_discovery_error
    assert result.bot_gateways_restarted == []


def test_pull_gateway_kickstart_respects_killswitch(
    tmp_path: Path, monkeypatch
):
    """EVOLVE_PULLER_AUTO_RESTART=0 disables BOTH admin-daemon restarts
    AND bot-gateway kicks. Pin the symmetry — operators flipping the
    switch as a mitigation expect everything pull-driven to stop."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "0")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    discovery_calls: list[bool] = []
    def fake_discover():
        discovery_calls.append(True)
        return ["ai.openclaw.team_bot_a-gateway"]
    kicks: list[str] = []
    def fake_kick(label):
        kicks.append(label)
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
        )

    assert result.success is True
    assert result.plugin_rebuilt is True
    # Discovery + kicks both blocked by the switch.
    assert discovery_calls == []
    assert kicks == []
    assert result.bot_gateways_restarted == []
    assert any("bot-gateway restart skipped" in s for s in result.steps)


def test_pull_gateway_stagger_does_not_sleep_for_single_or_empty(
    tmp_path: Path, monkeypatch
):
    """For a 1-bot pod, there's nothing to space against — sleep_fn must
    not fire. (n-1 sleeps for n kickstarts; n=1 → 0 sleeps.)"""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before, after = "a" * 40, "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit\n", ""),
        "diff": (0, "packages/plugin/src/tools/Telemetry.ts", ""),
        "update-index": (0, "", ""),
    })
    def fake_rebuild():
        return True, "rebuilt + staged"
    def fake_discover():
        return ["ai.openclaw.team_bot_a-gateway"]
    def fake_kick(label):
        return True, "ok"
    sleeps: list[float] = []

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            rebuild_plugin_fn=fake_rebuild,
            kickstart_fn=fake_kick,
            gateway_discovery_fn=fake_discover,
            gateway_sleep_fn=lambda s: sleeps.append(s),
        )

    assert result.success is True
    assert result.bot_gateways_restarted == ["ai.openclaw.team_bot_a-gateway"]
    assert sleeps == []


def test_format_for_log_surfaces_kicked_bot_gateways():
    """The puller log line is the only operator-visible signal that a
    plugin rebuild propagated to the bot gateways. Short bot names go
    in the log (team_bot_a, admin_bot) — the launchd labels are noisy."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        plugin_rebuilt=True,
        bot_gateways_restarted=[
            "ai.openclaw.evolve-gateway",
            "ai.openclaw.team_bot_a-gateway",
            "ai.openclaw.admin_bot-gateway",
        ],
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "kicked bot gateways: evolve, team_bot_a, admin_bot" in out


def test_format_for_log_surfaces_bot_gateway_restart_errors():
    """A kickstart failure must appear as a WARN line so the operator's
    grep catches it — silent failures here would reproduce the PR #1639
    gap shape (plugin rebuilt, gateway never picked up the new dist)."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        bot_gateway_restart_errors={
            "ai.openclaw.admin_bot-gateway": "rc=1: process held",
        },
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "WARN bot-gateway restart admin_bot failed" in out
    assert "process held" in out


def test_format_for_log_surfaces_bot_gateway_discovery_error():
    """If discovery couldn't run (permissions, missing dir), operators
    need to see that distinct from a per-gateway failure — the
    mitigation is different (fix perms vs. re-run kickstart)."""
    r = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        bot_gateway_discovery_error="PermissionError: stat",
    )
    out = repo_puller.format_for_log(r, quiet=True)
    assert "WARN bot-gateway discovery failed" in out
    assert "PermissionError" in out


def test_restart_bot_gateways_invokes_in_order_with_stagger():
    """Lower-level helper test: pin that kickstarts fire in the input
    list's order and that the staggered sleeps land BETWEEN kicks
    (not before the first, not after the last). The interleaving is
    what prevents thundering-herd while still bounding total wall time."""
    events: list[tuple[str, object]] = []
    def fake_kick(label):
        events.append(("kick", label))
        return True, "ok"
    def fake_sleep(s):
        events.append(("sleep", s))

    restarted, errors = repo_puller._restart_bot_gateways(
        ["a", "b", "c"],
        kickstart_fn=fake_kick,
        stagger_seconds=1.5,
        sleep_fn=fake_sleep,
    )

    assert restarted == ["a", "b", "c"]
    assert errors == {}
    assert events == [
        ("kick", "a"),
        ("sleep", 1.5),
        ("kick", "b"),
        ("sleep", 1.5),
        ("kick", "c"),
    ]


def test_restart_bot_gateways_continues_after_failure():
    """A failed kickstart in the middle must not block the rest of the
    list — best-effort, partial-success semantics. The failing label
    lands in `errors`, the successful ones in `restarted`."""
    def fake_kick(label):
        if label == "b":
            return False, "rc=1: held"
        return True, "ok"

    restarted, errors = repo_puller._restart_bot_gateways(
        ["a", "b", "c"],
        kickstart_fn=fake_kick,
        stagger_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert restarted == ["a", "c"]
    assert errors == {"b": "rc=1: held"}


# ── Sudoers auto-refresh on setup_wizard.py change ────────────────────────


def test_paths_touch_setup_wizard_recognises_setup_wizard_py():
    assert repo_puller._paths_touch_setup_wizard([
        "packages/admin/evolve_admin/setup_wizard.py",
    ]) is True
    assert repo_puller._paths_touch_setup_wizard([
        "packages/admin/evolve_admin/setup_wizard.py",
        "docs/CHANGELOG.md",
    ]) is True
    # Tolerant of future refactors that split the module — any
    # setup_wizard_*.py sibling under the admin package matches.
    assert repo_puller._paths_touch_setup_wizard([
        "packages/admin/evolve_admin/setup_wizard_sudoers.py",
    ]) is True


def test_paths_touch_setup_wizard_ignores_unrelated_paths():
    assert repo_puller._paths_touch_setup_wizard([
        "packages/admin/evolve_admin/deploy.py",
        "packages/admin/evolve_admin/cli.py",
        "docs/CHANGELOG.md",
    ]) is False
    # A test file that imports setup_wizard but isn't setup_wizard
    # itself shouldn't trigger the refresh.
    assert repo_puller._paths_touch_setup_wizard([
        "packages/admin/tests/test_setup_wizard.py",
    ]) is False


def _drift_git(setup_wizard_changed: bool):
    """A git runner whose pulled diff does / doesn't touch setup_wizard.py."""
    diff_files = (
        "packages/admin/evolve_admin/setup_wizard.py\nREADME.md"
        if setup_wizard_changed
        else "packages/admin/evolve_admin/deploy.py\ndocs/CHANGELOG.md"
    )
    return _make_git_runner({
        "rev-parse": [(0, "a" * 40, ""), (0, "b" * 40, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, diff_files, ""),
    })


def test_pull_fires_signal_on_sudoers_drift(tmp_path: Path):
    """setup_wizard.py changed + the installed sudoers lags the render →
    fire sudoers_refresh_failed (grants dormant) and surface a DRIFT step.
    The puller does NOT install (Option B, #2759 — evolve can't self-grant)."""
    fake = _drift_git(setup_wizard_changed=True)
    observed: list = []
    resolved: list = []

    def fake_drift(shared_dir):
        return False, "installed lags render — run refresh-sudoers as root"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "observe_sudoers_refresh_failed_signal",
                      lambda **kw: observed.append(kw)), \
         patch.object(repo_puller, "resolve_sudoers_refresh_signal",
                      lambda *a, **kw: resolved.append(1)):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=fake_drift)

    assert result.success is True
    assert result.sudoers_refresh_attempted is True
    assert result.sudoers_refresh_ok is False
    assert len(observed) == 1 and resolved == []
    assert any("DRIFT" in s for s in result.steps)


def test_pull_resolves_signal_when_sudoers_in_sync(tmp_path: Path):
    """In sync → RESOLVE any firing Signal. This is the cry-wolf fix: the old
    hook only resolved on its own (impossible-as-evolve) install success, so a
    manual fix left the Signal stuck firing until it became ignored noise."""
    fake = _drift_git(setup_wizard_changed=True)
    observed: list = []
    resolved: list = []

    def fake_drift(shared_dir):
        return True, "in sync"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "observe_sudoers_refresh_failed_signal",
                      lambda **kw: observed.append(kw)), \
         patch.object(repo_puller, "resolve_sudoers_refresh_signal",
                      lambda *a, **kw: resolved.append(1)):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=fake_drift)

    assert result.sudoers_refresh_attempted is True
    assert result.sudoers_refresh_ok is True
    assert len(resolved) == 1 and observed == []
    assert any("in sync" in s for s in result.steps)


def test_pull_runs_drift_check_when_signal_firing_even_if_untouched(tmp_path: Path):
    """Even when setup_wizard.py didn't change, a still-firing Signal makes the
    puller re-check — so an operator's manual fix is noticed (Signal resolved)
    on the next tick rather than firing forever."""
    fake = _drift_git(setup_wizard_changed=False)
    calls: list = []

    def fake_drift(shared_dir):
        calls.append(shared_dir)
        return True, "in sync"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "_sudoers_drift_signal_firing", lambda *_a: True), \
         patch.object(repo_puller, "resolve_sudoers_refresh_signal", lambda *a, **kw: None):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=fake_drift)

    assert result.sudoers_refresh_attempted is True
    assert len(calls) == 1


def test_pull_skips_drift_check_when_untouched_and_not_firing(tmp_path: Path):
    """No setup_wizard change AND no firing Signal → skip the render-cost drift
    check entirely (the common steady-state pull)."""
    fake = _drift_git(setup_wizard_changed=False)
    calls: list = []

    def fake_drift(shared_dir):
        calls.append(shared_dir)
        return True, "in sync"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "_sudoers_drift_signal_firing", lambda *_a: False):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=fake_drift)

    assert result.success is True
    assert result.sudoers_refresh_attempted is False
    assert calls == []


def test_pull_drift_unknown_leaves_signal_untouched(tmp_path: Path):
    """Render unavailable (None) → neither fire nor resolve: don't cry-wolf, and
    don't clear a genuine firing Signal on a transient render failure."""
    fake = _drift_git(setup_wizard_changed=True)
    observed: list = []
    resolved: list = []

    def fake_drift(shared_dir):
        return None, "render unavailable (openclaw CLI not discoverable)"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True), \
         patch.object(repo_puller, "observe_sudoers_refresh_failed_signal",
                      lambda **kw: observed.append(kw)), \
         patch.object(repo_puller, "resolve_sudoers_refresh_signal",
                      lambda *a, **kw: resolved.append(1)):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=fake_drift)

    assert result.sudoers_refresh_attempted is True
    assert result.sudoers_refresh_ok is False
    assert observed == [] and resolved == []
    assert any("skipped" in s for s in result.steps)


# ── drift-detection unit tests ────────────────────────────────────────────────


def _write_marker(shared_dir: Path, content: str) -> None:
    import hashlib
    state = shared_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "sudoers-installed.sha256").write_text(
        hashlib.sha256(content.encode("utf-8")).hexdigest() + "\n"
    )


def test_sudoers_drift_check_in_sync(tmp_path: Path, monkeypatch):
    """Marker hash matches the render → in sync."""
    from evolve_admin import setup_wizard as _sw
    monkeypatch.setattr(_sw, "_render_evolve_sudoers", lambda: "GRANTS v1\n")
    _write_marker(tmp_path, "GRANTS v1\n")
    in_sync, info = repo_puller._sudoers_drift_check(tmp_path)
    assert in_sync is True, info


def test_sudoers_drift_check_detects_drift(tmp_path: Path, monkeypatch):
    """Marker missing OR mismatched → DRIFT with an actionable message."""
    from evolve_admin import setup_wizard as _sw
    monkeypatch.setattr(_sw, "_render_evolve_sudoers", lambda: "GRANTS v2\n")
    # No marker at all (fresh / never refreshed via new code) → drift.
    in_sync, info = repo_puller._sudoers_drift_check(tmp_path)
    assert in_sync is False
    assert "refresh-sudoers" in info
    # Stale marker (old content) → still drift.
    _write_marker(tmp_path, "GRANTS v1\n")
    in_sync, _ = repo_puller._sudoers_drift_check(tmp_path)
    assert in_sync is False


def test_sudoers_drift_check_unknown_when_render_unavailable(tmp_path: Path, monkeypatch):
    """Render returns None (openclaw not discoverable) → unknown, NOT drift —
    so a transient render failure can't cry-wolf or clear a real Signal."""
    from evolve_admin import setup_wizard as _sw
    monkeypatch.setattr(_sw, "_render_evolve_sudoers", lambda: None)
    in_sync, info = repo_puller._sudoers_drift_check(tmp_path)
    assert in_sync is None
    assert "unavailable" in info


def test_sudoers_drift_signal_firing(tmp_path: Path):
    """True iff a sudoers_refresh_failed Signal is active in the store.

    The gate asks signals.store — the spec's signature_index.json was never
    implemented, and the original index-file read here silently returned
    False forever, so a manually-refreshed sudoers never got its Signal
    auto-resolved on subsequent no-change pulls (VPS, 2026-07-29)."""
    (tmp_path / "signals" / "firing").mkdir(parents=True, exist_ok=True)
    assert repo_puller._sudoers_drift_signal_firing(tmp_path) is False  # empty store
    repo_puller.observe_sudoers_refresh_failed_signal(
        error="drift detail", head="f" * 40, shared_dir=tmp_path,
    )
    if not list((tmp_path / "signals" / "firing").glob("*.json")):
        import pytest
        pytest.skip("signals.store not importable in this test env")
    assert repo_puller._sudoers_drift_signal_firing(tmp_path) is True
    repo_puller.resolve_sudoers_refresh_signal(tmp_path)
    assert repo_puller._sudoers_drift_signal_firing(tmp_path) is False


def test_write_evolve_sudoers_records_marker(tmp_path: Path, monkeypatch):
    """A successful install records the content hash so the puller can detect
    drift and AUTO-RESOLVE the Signal once the operator refreshes."""
    import hashlib

    from evolve_admin import setup_wizard as _sw
    import evolve_config as _ec
    monkeypatch.setattr(_ec, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(_ec, "resolve_network_path", lambda *a, **k: None)
    monkeypatch.setattr(_ec, "get_shared_dir", lambda *a, **k: str(tmp_path))

    _sw._record_installed_sudoers_marker("SUDOERS CONTENT\n")
    marker = tmp_path / "state" / "sudoers-installed.sha256"
    assert marker.read_text().strip() == hashlib.sha256(b"SUDOERS CONTENT\n").hexdigest()


def test_pull_sudoers_refresh_runs_before_pip_install_and_daemon_restart(
    tmp_path: Path, monkeypatch,
):
    """Order matters: sudoers must be refreshed BEFORE pip install and
    daemon restart. A PR that adds a new sudoers grant alongside a new
    pip dep would otherwise hit the missing grant when pip-install
    tries to escalate; same for a daemon's first post-restart sudo."""
    monkeypatch.setenv(repo_puller.AUTO_RESTART_ENV, "1")
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0,
            "packages/admin/evolve_admin/setup_wizard.py\n"
            "packages/admin/pyproject.toml\n"
            "packages/admin/evolve_admin/server.py", ""),
    })
    sequence: list[str] = []

    def fake_refresh(shared_dir) -> tuple[bool, str]:
        sequence.append("sudoers")
        return True, "in sync"

    def fake_pip(repo: Path) -> tuple[bool, str]:
        sequence.append("pip")
        return True, "ok"

    def fake_kick(label: str) -> tuple[bool, str]:
        sequence.append(f"kick:{label}")
        return True, "ok"

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(
            repo=tmp_path,
            sudoers_refresh_fn=fake_refresh,
            pip_install_fn=fake_pip,
            kickstart_fn=fake_kick,
        )

    assert result.success is True
    assert "sudoers" in sequence
    assert "pip" in sequence
    assert any(s.startswith("kick:") for s in sequence)
    sudoers_idx = sequence.index("sudoers")
    pip_idx = sequence.index("pip")
    first_kick_idx = next(i for i, s in enumerate(sequence) if s.startswith("kick:"))
    assert sudoers_idx < pip_idx < first_kick_idx, (
        f"sudoers refresh must precede pip install must precede daemon "
        f"kickstart; sequence={sequence}"
    )


def test_pull_sudoers_refresh_exception_does_not_crash_pull(tmp_path: Path):
    """A raised exception from the refresh fn (e.g. setup_wizard import
    error on a half-rolled-out venv) must be caught and surfaced as a
    structured failure on the result, not propagated up to crash the
    daemon."""
    before = "a" * 40
    after = "b" * 40
    fake = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/admin/evolve_admin/setup_wizard.py", ""),
    })

    def raising_refresh(shared_dir) -> tuple[bool, str]:
        raise RuntimeError("boom")

    with patch.object(repo_puller, "_git", fake), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, sudoers_refresh_fn=raising_refresh)

    assert result.success is True
    assert result.sudoers_refresh_attempted is True
    assert result.sudoers_refresh_ok is False
    assert "RuntimeError" in result.sudoers_refresh_info
    assert "boom" in result.sudoers_refresh_info


def test_format_for_log_surfaces_sudoers_refresh_outcome():
    """Operator-visible log lines: success → one-line confirmation;
    failure → WARN with the recovery hint (`refresh-sudoers` manually)."""
    ok_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        sudoers_refresh_attempted=True,
        sudoers_refresh_ok=True,
        sudoers_refresh_info="ok",
    )
    out = repo_puller.format_for_log(ok_result)
    assert "in sync" in out

    fail_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        sudoers_refresh_attempted=True,
        sudoers_refresh_ok=False,
        sudoers_refresh_info=(
            "installed /etc/sudoers.d/evolve lags the rendered template — grants "
            "dormant. Run `sudo evolve-admin refresh-sudoers` as root."
        ),
    )
    out = repo_puller.format_for_log(fail_result)
    assert "WARN sudoers:" in out
    assert "dormant" in out
    assert "refresh-sudoers" in out   # recovery hint (carried in the drift info)


def test_write_evolve_sudoers_accepts_initiated_by_kwarg():
    """The puller's auto-refresh wrapper calls
    ``setup_wizard._write_evolve_sudoers(initiated_by="repo-puller")``
    so the admin-actions.jsonl entry distinguishes wizard / cli /
    repo-puller invocations. PR #1909 landed the wrapper but the
    wizard helper's signature was never widened; every auto-refresh
    crashed with TypeError for 8 days and the first PR that added a
    new grant (f294255e) silently failed to install it. This pin
    catches the class without touching disk / sudo.
    """
    import inspect
    from evolve_admin.setup_wizard import _write_evolve_sudoers
    sig = inspect.signature(_write_evolve_sudoers)
    assert "initiated_by" in sig.parameters, (
        "_write_evolve_sudoers must accept initiated_by= kwarg — the "
        "puller's auto-refresh wrapper passes it and the TypeError is "
        "silenced into a WARN log line that operators don't tail."
    )
    p = sig.parameters["initiated_by"]
    assert p.default == "wizard", (
        f"initiated_by default should be 'wizard' (wizard is the most "
        f"common caller); got {p.default!r}"
    )


def test_observe_sudoers_refresh_failed_signal_writes_signal_file(tmp_path: Path):
    """When the auto-refresh wrapper fails, a Signal lands in the store
    so the Alerts UI and signal_notifier can surface it. Producer is
    `repo_puller_sudoers` (distinct from `repo_puller`) so the wedge
    sweep doesn't clobber it."""
    (tmp_path / "signals" / "firing").mkdir(parents=True, exist_ok=True)
    repo_puller.observe_sudoers_refresh_failed_signal(
        error="raised: TypeError: ...",
        head="f" * 40,
        shared_dir=tmp_path,
    )
    firing = list((tmp_path / "signals" / "firing").glob("*.json"))
    if not firing:
        import pytest
        pytest.skip("signals.store not importable in this test env")
    import json as _json
    payload = _json.loads(firing[0].read_text())
    assert payload.get("producer") == "repo_puller_sudoers"
    assert payload.get("type") == "sudoers_refresh_failed"
    assert "TypeError" in (payload.get("body") or "")
    assert (payload.get("details") or {}).get("recovery_command") == \
        "sudo evolve-admin refresh-sudoers"


def test_resolve_sudoers_refresh_signal_archives_firing(tmp_path: Path):
    """Once a subsequent pull's auto-refresh succeeds, the firing
    Signal is swept to archived."""
    (tmp_path / "signals" / "firing").mkdir(parents=True, exist_ok=True)
    repo_puller.observe_sudoers_refresh_failed_signal(
        error="raised: TypeError: ...",
        head="f" * 40,
        shared_dir=tmp_path,
    )
    firing_before = list((tmp_path / "signals" / "firing").glob("*.json"))
    if not firing_before:
        import pytest
        pytest.skip("signals.store not importable in this test env")
    resolved = repo_puller.resolve_sudoers_refresh_signal(tmp_path)
    assert len(resolved) == 1
    firing_after = list((tmp_path / "signals" / "firing").glob("*.json"))
    assert firing_after == []


def test_repo_puller_sudoers_reaches_chat_by_default():
    """Under deny-list-by-default, repo_puller_sudoers Signals reach
    operator chat automatically — no allowlist entry needed. The only
    requirement is that it is NOT in the direct-dispatch deny-list
    (which would silence it).

    repo_puller_sudoers uses signals.store.observe() only and never
    calls dispatcher.send, so there is no double-message risk."""
    from evolve_admin.alerts.signal_notifier import _DIRECT_DISPATCH_PRODUCERS
    assert "repo_puller_sudoers" not in _DIRECT_DISPATCH_PRODUCERS


def test_sudoers_self_refresh_grants_present_in_evolve_sudoers():
    """The puller's auto-refresh hook runs `_write_evolve_sudoers` as the
    evolve user. Without these four narrow grants (visudo -c on the temp
    file, /bin/cp to /etc/sudoers.d/evolve, chmod 440, chown root:wheel),
    the refresh fails at the first sudo call with 'evolve is not in the
    sudoers file' and the hook is a permanent no-op.

    Also pins the tempfile prefix: _write_evolve_sudoers must stage to
    /tmp/evolve-sudoers-*.sudoers so the cp/visudo grants stay narrow.
    Drift between the staging prefix and the grant pattern silently
    re-introduces the very footgun this hook closes."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers
    content = _render_evolve_sudoers()
    assert content is not None, "openclaw not discoverable at test time"
    needed = [
        "evolve ALL=(root) NOPASSWD: /usr/sbin/visudo -c -f /tmp/evolve-sudoers-*.sudoers",
        "evolve ALL=(root) NOPASSWD: /bin/cp /tmp/evolve-sudoers-*.sudoers /etc/sudoers.d/evolve",
        "evolve ALL=(root) NOPASSWD: /bin/chmod 440 /etc/sudoers.d/evolve",
        "evolve ALL=(root) NOPASSWD: /usr/sbin/chown root:wheel /etc/sudoers.d/evolve",
    ]
    for needle in needed:
        assert needle in content, (
            f"Expected sudoers grant for the puller's auto-refresh hook "
            f"is missing. Add this line in setup_wizard._render_evolve_sudoers:"
            f"\n  {needle}"
        )


# ── Lagging-bot redeploy sweep ────────────────────────────────────────────


def _write_install_json(
    shared_dir: Path, current_version: str, bot_versions: dict[str, str]
) -> None:
    """Helper: write a minimal install.json with the given per-bot stamps."""
    import json as _json
    payload = {
        "version": current_version,
        "bot_versions": {
            bid: {"version": v, "deployed_at": "2026-01-01T00:00:00Z"}
            for bid, v in bot_versions.items()
        },
    }
    (shared_dir / "install.json").write_text(_json.dumps(payload))


def _write_network_json(repo: Path, bots: dict[str, dict]) -> Path:
    """Helper: write config/network.json under repo with the given bots map."""
    import json as _json
    cfg_dir = repo / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    network_path = cfg_dir / "network.json"
    network_path.write_text(_json.dumps({
        "bots": bots,
        "members": list(bots.keys()),
    }))
    return network_path


def test_find_lagging_bots_returns_bots_with_stale_version(tmp_path: Path):
    _write_install_json(tmp_path, "2026.0603.2067", {
        "team_bot_a": "2026.0601.2010",  # lagging
        "team_bot_c": "2026.0603.2067",  # current
        "team_bot_b": "",                 # never stamped
    })
    lagging = repo_puller._find_lagging_bots(tmp_path, "2026.0603.2067")
    ids = sorted(b for b, _ in lagging)
    assert ids == ["team_bot_a", "team_bot_b"]


def test_find_lagging_bots_returns_empty_when_install_json_missing(tmp_path: Path):
    assert repo_puller._find_lagging_bots(tmp_path, "2026.0603.2067") == []


def test_find_lagging_bots_returns_empty_when_install_json_malformed(tmp_path: Path):
    (tmp_path / "install.json").write_text("{not valid json")
    assert repo_puller._find_lagging_bots(tmp_path, "2026.0603.2067") == []


def test_redeploy_lagging_bots_calls_deploy_then_record(tmp_path: Path, monkeypatch):
    """The sweep should call deploy_bot then record_bot_deploy for each
    lagging bot whose ledger entry exists in network.json."""
    from evolve_admin.deploy import DeployResult, EVOLVE_VERSION
    _write_install_json(tmp_path, EVOLVE_VERSION, {
        "team_bot_a": "0.0.0",     # lagging
        "team_bot_c": "0.0.0",   # lagging
        "team_bot_b": EVOLVE_VERSION,  # current — skip
    })
    _write_network_json(tmp_path, {
        "team_bot_a": {"role": "member", "port": 8901, "backupRepoUrl": "https://github.com/x/team-bot-a"},
        "team_bot_c": {"role": "member", "port": 8902, "backupRepoUrl": ""},
        "team_bot_b": {"role": "member", "port": 8903, "backupRepoUrl": ""},
    })

    deploy_calls: list[dict] = []
    record_calls: list[str] = []

    def fake_deploy(bot_id, role, port, network_path, dry_run=False, backup_repo_url=""):
        deploy_calls.append({
            "bot_id": bot_id, "role": role, "port": port,
            "backup_repo_url": backup_repo_url, "dry_run": dry_run,
        })
        return DeployResult(bot_id=bot_id, success=True)

    def fake_record(bot_id, shared_dir):
        record_calls.append(bot_id)

    succeeded, errors = repo_puller._redeploy_lagging_bots(
        repo=tmp_path, shared_dir=tmp_path,
        deploy_fn=fake_deploy, record_fn=fake_record,
    )

    assert sorted(succeeded) == ["team_bot_a", "team_bot_c"]
    assert errors == {}
    assert sorted(c["bot_id"] for c in deploy_calls) == ["team_bot_a", "team_bot_c"]
    # Per-bot config carried through
    by_bot = {c["bot_id"]: c for c in deploy_calls}
    assert by_bot["team_bot_a"]["port"] == 8901
    assert by_bot["team_bot_a"]["backup_repo_url"] == "https://github.com/x/team-bot-a"
    assert sorted(record_calls) == ["team_bot_a", "team_bot_c"]


def test_redeploy_lagging_bots_skips_bots_not_in_network_json(tmp_path: Path):
    """Stale install.json entries for removed bots shouldn't crash the
    sweep — flag them as errors and continue."""
    from evolve_admin.deploy import DeployResult, EVOLVE_VERSION
    _write_install_json(tmp_path, EVOLVE_VERSION, {
        "team_bot_a": "0.0.0",       # in network.json
        "phantom": "0.0.0",   # NOT in network.json (removed bot)
    })
    _write_network_json(tmp_path, {
        "team_bot_a": {"role": "member"},
    })

    deploy_calls: list[str] = []

    def fake_deploy(bot_id, **kw):
        deploy_calls.append(bot_id)
        return DeployResult(bot_id=bot_id, success=True)

    def fake_record(bot_id, shared_dir):
        pass

    succeeded, errors = repo_puller._redeploy_lagging_bots(
        repo=tmp_path, shared_dir=tmp_path,
        deploy_fn=fake_deploy, record_fn=fake_record,
    )

    assert succeeded == ["team_bot_a"]
    assert "phantom" in errors
    assert "not registered" in errors["phantom"]
    assert deploy_calls == ["team_bot_a"]


def test_redeploy_lagging_bots_captures_per_bot_failure(tmp_path: Path):
    """One bot's deploy_bot raising should NOT prevent the others from
    redeploying. Failures captured, sweep continues."""
    from evolve_admin.deploy import DeployResult, EVOLVE_VERSION
    _write_install_json(tmp_path, EVOLVE_VERSION, {
        "team_bot_a": "0.0.0",
        "team_bot_c": "0.0.0",
    })
    _write_network_json(tmp_path, {
        "team_bot_a": {"role": "member"},
        "team_bot_c": {"role": "member"},
    })

    def fake_deploy(bot_id, **kw):
        if bot_id == "team_bot_a":
            raise RuntimeError("simulated deploy failure")
        return DeployResult(bot_id=bot_id, success=True)

    def fake_record(bot_id, shared_dir):
        pass

    succeeded, errors = repo_puller._redeploy_lagging_bots(
        repo=tmp_path, shared_dir=tmp_path,
        deploy_fn=fake_deploy, record_fn=fake_record,
    )

    assert succeeded == ["team_bot_c"]
    assert "team_bot_a" in errors
    assert "simulated deploy failure" in errors["team_bot_a"]


def test_redeploy_lagging_bots_no_op_when_nothing_lags(tmp_path: Path):
    """If every bot already matches EVOLVE_VERSION, no deploys are made."""
    from evolve_admin.deploy import EVOLVE_VERSION
    _write_install_json(tmp_path, EVOLVE_VERSION, {
        "team_bot_a": EVOLVE_VERSION,
        "team_bot_c": EVOLVE_VERSION,
    })

    deploys: list[str] = []

    def fake_deploy(bot_id, **kw):
        deploys.append(bot_id)

    def fake_record(bot_id, shared_dir):
        pass

    succeeded, errors = repo_puller._redeploy_lagging_bots(
        repo=tmp_path, shared_dir=tmp_path,
        deploy_fn=fake_deploy, record_fn=fake_record,
    )

    assert succeeded == []
    assert errors == {}
    assert deploys == []


def test_redeploy_lagging_bots_handles_missing_network_json(tmp_path: Path):
    """When install.json shows lag but network.json is missing, the sweep
    reports an error rather than crashing."""
    from evolve_admin.deploy import EVOLVE_VERSION
    _write_install_json(tmp_path, EVOLVE_VERSION, {"team_bot_a": "0.0.0"})

    def fake_deploy(bot_id, **kw):
        raise AssertionError("deploy should never be called")

    def fake_record(bot_id, shared_dir):
        raise AssertionError("record should never be called")

    succeeded, errors = repo_puller._redeploy_lagging_bots(
        repo=tmp_path, shared_dir=tmp_path,
        deploy_fn=fake_deploy, record_fn=fake_record,
    )

    assert succeeded == []
    assert "__no_network_json__" in errors


def test_format_for_log_surfaces_lagging_bot_redeploys():
    """Operator-facing log line shows the redeploy sweep result so a
    successful catch-up is visible without grepping individual steps."""
    ok_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        lagging_bots_redeployed=["team_bot_a", "team_bot_c"],
    )
    out = repo_puller.format_for_log(ok_result)
    assert "redeployed 2 lagging bot(s)" in out
    assert "team_bot_a" in out and "team_bot_c" in out

    fail_result = repo_puller.PullResult(
        success=True,
        head_before="a" * 40,
        head_after="b" * 40,
        commits_advanced=1,
        lagging_bot_deploy_errors={"team_bot_a": "RuntimeError: oops"},
    )
    out = repo_puller.format_for_log(fail_result)
    assert "WARN redeploy team_bot_a failed" in out
    assert "RuntimeError" in out


# ── Signal-store integration (Evo-confabulation fix, 2026-06-06) ──────────
#
# These tests pin the contract that every wedge writes a structured Signal
# carrying diagnostic evidence (last_stderr_tail, blocking_paths, etc.) so
# downstream consumers (Evo, Alerts UI) read evidence instead of guessing.
# The 2026-06-06 incident — Evo confabulating "SSH key missing" because the
# pre-Signal repo-puller only emitted a one-line chat alert — is the
# motivating regression these tests guard against.

import sys as _sys  # noqa: E402

_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
if str(_ANALYZER) not in _sys.path:
    _sys.path.insert(0, str(_ANALYZER))

from signals import store as _signals_store  # noqa: E402


_BLOCKING_ERROR = (
    "From github.com:evolve-ops/evolve\n"
    " * branch              main       -> FETCH_HEAD\n"
    "   e618aeeb..d98597c5  main       -> origin/main\n"
    "error: Your local changes to the following files would be overwritten by merge:\n"
    "\tpackages/admin/evolve_admin/deploy.py\n"
    "Please commit your changes or stash them before you merge.\n"
    "Aborting"
)

_UNTRACKED_ERROR = (
    "error: The following untracked working tree files would be overwritten by merge:\n"
    "\tpackages/analyzer/extract_tuples.py\n"
    "\tpackages/analyzer/observations/llm_extractor.py\n"
    "Please move or remove them before you merge.\n"
    "Aborting"
)


def test_parse_blocking_paths_finds_modified_files():
    """The 2026-06-06 wedge shape: modified file blocks fast-forward.
    Without this parse, the upstream-touches lookup can't run and the
    Signal misses its highest-leverage field."""
    paths = repo_puller._parse_blocking_paths(_BLOCKING_ERROR)
    assert paths == ["packages/admin/evolve_admin/deploy.py"]


def test_parse_blocking_paths_finds_untracked_files():
    paths = repo_puller._parse_blocking_paths(_UNTRACKED_ERROR)
    assert paths == [
        "packages/analyzer/extract_tuples.py",
        "packages/analyzer/observations/llm_extractor.py",
    ]


def test_parse_blocking_paths_returns_empty_for_unrelated_error():
    """Network failures, non-fast-forward, missing repo — all real wedge
    shapes that don't list blocking paths. Returning [] lets the caller
    skip the upstream-lookup step instead of running git on garbage."""
    assert repo_puller._parse_blocking_paths("fatal: Not possible to fast-forward") == []
    assert repo_puller._parse_blocking_paths("") == []
    assert repo_puller._parse_blocking_paths(None or "") == []  # mypy guard


def test_build_wedge_signal_details_never_raises_when_git_fails(tmp_path: Path):
    """Signal emission must never crash the daemon. Every git subcommand
    inside _build_wedge_signal_details has to be wrapped — if status,
    rev-list, or log fails, the field is omitted and the rest survives."""
    def boom(repo, args):
        raise RuntimeError("git not on PATH")
    with patch.object(repo_puller, "_git", boom):
        details = repo_puller._build_wedge_signal_details(
            tmp_path, _BLOCKING_ERROR,
        )
    # Minimum guaranteed fields land regardless.
    assert details["repo_path"] == str(tmp_path)
    assert details["last_stderr_tail"].endswith("Aborting")
    assert details["blocking_paths"] == ["packages/admin/evolve_admin/deploy.py"]
    # Git-dependent fields stay absent rather than emitting placeholder values.
    assert "git_status_porcelain" not in details
    assert "fetch_succeeded" not in details
    assert "upstream_commits_touching_blocking_paths" not in details


def test_build_wedge_signal_details_populates_upstream_touches(tmp_path: Path):
    """The smoking-gun field that disambiguates 'discard local' from
    'stash, pull, pop' — when upstream has commits touching the blocking
    path, a consumer should recommend ``git checkout -- <path>`` instead
    of the stash dance Evo got wrong on 2026-06-06."""
    upstream_log = (
        "d98597c5 fix(perms): add config_intents to evo write-ACL contract (#2331)"
    )

    def fake_git(repo, args):
        if args[:1] == ["status"]:
            return 0, " M packages/admin/evolve_admin/deploy.py", ""
        if args[:1] == ["rev-list"]:
            return 0, "1", ""
        if args[:1] == ["log"]:
            return 0, upstream_log, ""
        return 1, "", "unexpected"

    with patch.object(repo_puller, "_git", fake_git):
        details = repo_puller._build_wedge_signal_details(
            tmp_path, _BLOCKING_ERROR,
        )

    assert details["fetch_succeeded"] is True
    assert details["upstream_commits_ahead"] == 1
    assert details["git_status_porcelain"] == " M packages/admin/evolve_admin/deploy.py"
    upstream = details["upstream_commits_touching_blocking_paths"]
    assert "packages/admin/evolve_admin/deploy.py" in upstream
    assert upstream["packages/admin/evolve_admin/deploy.py"] == [upstream_log]


def test_tick_failed_pull_emits_signal_with_diagnostic_payload(tmp_path: Path):
    """End-to-end: a failed tick writes a Signal whose details carry the
    fields Evo needs. This is the contract the 2026-06-06 incident exposed
    as missing — pin it so it can't regress silently."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    def fake_git(r, args):
        head = args[:1]
        if head == ["rev-parse"]:
            return 0, "a" * 40, ""
        if head == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        if head == ["status"]:
            return 0, " M packages/admin/evolve_admin/deploy.py", ""
        if head == ["rev-list"]:
            return 0, "1", ""
        if head == ["log"]:
            return 0, "d98597c5 fix(perms): add config_intents", ""
        return 1, "", f"unexpected: {args}"

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo)

    assert result.pull.success is False
    fired = list(_signals_store.iter_active(
        shared_dir, producer="repo_puller", state="firing",
    ))
    assert len(fired) == 1
    sig = fired[0]
    assert sig.type == "repo_puller_wedged"
    assert sig.scope == "pod"
    assert sig.severity == "warn"
    assert "repo-puller wedged" in sig.title
    d = sig.details
    assert d["repo_path"] == str(repo)
    assert "Your local changes" in d["last_stderr_tail"]
    assert d["blocking_paths"] == ["packages/admin/evolve_admin/deploy.py"]
    assert d["fetch_succeeded"] is True
    assert d["upstream_commits_ahead"] == 1
    assert "packages/admin/evolve_admin/deploy.py" in d["upstream_commits_touching_blocking_paths"]
    assert d["git_status_porcelain"].startswith(" M ")
    assert "incident_md" in d
    # Full path travels alongside the bare name — the records live under
    # {shared_dir}, so name-only is no longer locatable.
    assert d["incident_path"].endswith(d["incident_md"])


def test_tick_renders_discard_recipe_when_upstream_touches_blocking_path(tmp_path: Path):
    """End-to-end: when ``git log HEAD..origin/main -- <blocking-path>``
    returns commits, the incident-md the wedge produced must recommend
    ``git checkout -- <path>`` (discard) instead of ``git stash push``.

    This is the contract that prevents the 2026-06-06 confabulation
    shape from re-emerging via a different operator surface. The Signal
    payload + the incident-md must agree on the recipe shape because
    they're built from the same git probes.
    """
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    upstream_log = (
        "d98597c5 fix(perms): add config_intents to evo write-ACL contract (#2331)"
    )

    def fake_git(r, args):
        head = args[:1]
        if head == ["rev-parse"]:
            return 0, "a" * 40, ""
        if head == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        if head == ["status"]:
            return 0, " M packages/admin/evolve_admin/deploy.py", ""
        if head == ["rev-list"]:
            return 0, "1", ""
        if head == ["log"]:
            return 0, upstream_log, ""
        return 1, "", f"unexpected: {args}"

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo)

    assert result.issue_path is not None
    text = result.issue_path.read_text()
    # Discard recipe wins because upstream has touched the blocking path.
    assert "git diff origin/main -- packages/admin/evolve_admin/deploy.py" in text
    assert "git checkout -- packages/admin/evolve_admin/deploy.py" in text
    # And the stash recipe is NOT recommended — that's the bug the Evo
    # incident exposed.
    assert "git stash push" not in text
    # Upstream commit listed inline so the operator can spot the duplicate-PR
    # case without re-running ``git log``.
    assert "d98597c5 fix(perms): add config_intents" in text


def test_tick_renders_stash_recipe_when_upstream_has_no_touching_commits(tmp_path: Path):
    """The complementary contract: when upstream has NOT touched the
    blocking path (real WIP on the deploy box that hasn't reached origin),
    the recipe must stay on the stash shape so the operator preserves
    their work instead of discarding it.

    ``git rev-list HEAD..origin/main`` returns >0 (the fetch worked) but
    ``git log HEAD..origin/main -- <path>`` returns empty for the blocking
    path → ``upstream_commits_touching_blocking_paths`` is absent.
    """
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    def fake_git(r, args):
        head = args[:1]
        if head == ["rev-parse"]:
            return 0, "a" * 40, ""
        if head == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        if head == ["status"]:
            return 0, " M packages/admin/evolve_admin/deploy.py", ""
        if head == ["rev-list"]:
            return 0, "3", ""
        if head == ["log"]:
            # Upstream is ahead in general but has NOT touched this file —
            # the local diff is real WIP, not a duplicate of a merged PR.
            return 0, "", ""
        return 1, "", f"unexpected: {args}"

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo)

    assert result.issue_path is not None
    text = result.issue_path.read_text()
    # Stash recipe is correct here — preserves operator WIP.
    assert "git stash push" in text
    # Discard recipe markers must NOT appear, or the operator might trash
    # their work on bad advice.
    assert "git checkout -- " not in text
    assert "git diff origin/main -- " not in text


def test_tick_repeated_failure_bumps_observation_count_not_duplicates(tmp_path: Path):
    """Signature dedup means two ticks → one Signal, observation_count=2.
    Same shape as the chat-side dedup; without this, every 15-min puller
    failure would mint a fresh row on the Alerts page and overwhelm it."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    def fake_git(r, args):
        if args[:1] == ["rev-parse"]:
            return 0, "a" * 40, ""
        if args[:1] == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        return 0, "", ""

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    t1 = dt.datetime(2026, 6, 7, 7, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 6, 7, 7, 15, 0, tzinfo=dt.timezone.utc)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo, now=t1)
        repo_puller.tick(repo=repo, now=t2)

    fired = list(_signals_store.iter_active(
        shared_dir, producer="repo_puller", state="firing",
    ))
    assert len(fired) == 1
    assert fired[0].observation_count == 2


def test_tick_successful_pull_resolves_firing_wedge_signal(tmp_path: Path):
    """The recovery path: once a pull succeeds, the firing Signal must
    auto-archive so the Alerts UI clears and Evo stops surfacing the
    wedge in answers. Without this, every fixed wedge leaves a permanent
    'firing' row behind."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    # Step 1: induce a wedge so a Signal exists.
    def fake_git_fail(r, args):
        if args[:1] == ["rev-parse"]:
            return 0, "a" * 40, ""
        if args[:1] == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        return 0, "", ""

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    with patch.object(repo_puller, "_git", fake_git_fail), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo)

    fired = list(_signals_store.iter_active(
        shared_dir, producer="repo_puller", state="firing",
    ))
    assert len(fired) == 1, "precondition: wedge signal must be firing"

    # Step 2: pull succeeds → resolve.
    sha = "a" * 40

    def fake_git_ok(r, args):
        h = args[:1]
        if h == ["rev-parse"]:
            return 0, sha, ""
        if h == ["pull"]:
            return 0, "Already up to date.", ""
        if h == ["stash"]:
            return 0, "", ""
        return 0, "", ""

    with patch.object(repo_puller, "_git", fake_git_ok), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo)

    fired_after = list(_signals_store.iter_active(
        shared_dir, producer="repo_puller", state="firing",
    ))
    assert fired_after == [], "wedge Signal should have auto-resolved on success"


def test_observe_wedge_signal_silently_noops_when_shared_dir_unwritable(tmp_path: Path):
    """The daemon's correctness is more important than the Signal side-
    effect. If the shared_dir is unreachable, observe_wedge_signal must
    swallow the error rather than propagate."""
    # Point at a child of a file (not a dir) so any write attempt fails.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("blocker")
    bad_shared = blocker / "shared"

    # Must not raise.
    repo_puller.observe_wedge_signal(
        repo=tmp_path / "fake-repo",
        error=_BLOCKING_ERROR,
        head_before="a" * 40,
        branch="main",
        issue_path=None,
        shared_dir=bad_shared,
    )


# ── Recovery notification (closing-bracket to the wedge alert) ────────────


def _routine_success_git_runner(sha: str = "a" * 40):
    """Git runner that simulates a routine successful no-op tick."""
    return _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
        "stash": (0, "", ""),
    })


def test_tick_success_with_no_prior_wedge_sends_no_recovery_message(tmp_path: Path):
    """The thing this feature is designed to avoid: green-on-green pings.
    A healthy pod ticks every 15 minutes; if recovery fires on every
    success, the operator gets 96 'puller is fine!' messages per day.
    Recovery must only fire on a tick that actually transitions a firing
    wedge Signal."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    calls: list[tuple] = []

    def fake_recovery(repo_arg, pr):
        calls.append((repo_arg, pr))
        return True, ""

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    with patch.object(repo_puller, "_git", _routine_success_git_runner()), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)

    assert result.pull.success is True
    assert calls == [], "recovery notifier must not fire on routine success"
    assert result.recovery_notified is False


def test_tick_success_after_wedge_sends_exactly_one_recovery_message(tmp_path: Path):
    """The operator got the red 'puller wedged' alert; they need the
    green 'puller is back' closing bracket. Pin the full wedge → recovery
    cycle end-to-end so the integration can't silently regress."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    calls: list[tuple] = []

    def fake_recovery(repo_arg, pr):
        calls.append((repo_arg, pr))
        return True, ""

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    # Step 1: induce a wedge.
    def fake_git_fail(r, args):
        if args[:1] == ["rev-parse"]:
            return 0, "a" * 40, ""
        if args[:1] == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        return 0, "", ""

    with patch.object(repo_puller, "_git", fake_git_fail), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)
    assert calls == [], "wedge tick must not trigger recovery dispatch"

    # Step 2: pull succeeds → recovery fires exactly once. Use distinct
    # before/after SHAs so pull() detects an advance and surfaces
    # commits_advanced — the recovery message uses that to render
    # "advanced N commits" instead of the "already up to date" fallback.
    before = "a" * 40
    head_after = "b" * 40
    advance_runner = _make_git_runner({
        "rev-parse": [(0, before, ""), (0, head_after, "")],
        "pull": (0, "Updating a..b\nFast-forward", ""),
        "log": (0, "b1234567 fix(thing): something", ""),
        "diff": (0, "", ""),
        "stash": (0, "", ""),
    })

    with patch.object(repo_puller, "_git", advance_runner), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)

    assert result.pull.success is True
    assert result.recovery_notified is True
    assert len(calls) == 1, f"expected one recovery call, got {len(calls)}"
    sent_repo, sent_pr = calls[0]
    assert sent_repo == repo
    assert sent_pr.head_after == head_after
    assert sent_pr.commits_advanced == 1


def test_tick_two_consecutive_successful_ticks_after_wedge_recover_only_once(tmp_path: Path):
    """Once a wedge resolves on tick N, tick N+1's successful pull must
    NOT send a second recovery message. sweep_resolve enforces this
    naturally (the second sweep finds nothing firing); pinning the
    behavior catches a regression where someone wires recovery to fire
    on every success."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    calls: list[tuple] = []

    def fake_recovery(repo_arg, pr):
        calls.append((repo_arg, pr))
        return True, ""

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    def fake_git_fail(r, args):
        if args[:1] == ["rev-parse"]:
            return 0, "a" * 40, ""
        if args[:1] == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        return 0, "", ""

    with patch.object(repo_puller, "_git", fake_git_fail), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)

    with patch.object(repo_puller, "_git", _routine_success_git_runner()), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)
    assert len(calls) == 1, "first success after wedge must dispatch"

    with patch.object(repo_puller, "_git", _routine_success_git_runner()), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo, recovery_notifier=fake_recovery)
    assert len(calls) == 1, "second success must NOT re-dispatch"


def test_tick_recovery_dispatcher_failure_does_not_crash_daemon(tmp_path: Path):
    """Same contract as the wedge notifier: a misbehaving recovery
    dispatcher must not propagate into the daemon's exit code."""
    repo, _ = _make_repo_layout(tmp_path)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    def fake_git_fail(r, args):
        if args[:1] == ["rev-parse"]:
            return 0, "a" * 40, ""
        if args[:1] == ["pull"]:
            return 1, "", _BLOCKING_ERROR
        return 0, "", ""

    with patch.object(repo_puller, "_git", fake_git_fail), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        repo_puller.tick(repo=repo)

    def boom(repo_arg, pr):
        raise RuntimeError("dispatcher exploded")

    with patch.object(repo_puller, "_git", _routine_success_git_runner()), \
         patch.object(repo_puller, "DEFAULT_SHARED_DIR", shared_dir), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, recovery_notifier=boom)

    assert result.pull.success is True
    assert result.recovery_notified is False
    assert "RuntimeError" in result.recovery_notify_error


# ── Builtin Spec re-seed hook (deploy-time gallery propagation) ────────────
#
# Repo gallery edits don't reach a deployed pod's bound builtin Specs on
# their own (a gallery install binds the pre-existing builtin and never
# re-reads the repo package), so the puller re-seeds the builtin tier each
# tick. Root cause of the 2026-06-12 U1 morning-briefing delivery bug (#2792).

_INITIAL_SPEC_VERSION = "2026.05.20-1.0"


def _build_reseed_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Lay out a deploy-checkout repo (with a gallery package) + a pod shared
    dir holding a STRANDED builtin Spec (no seed-provenance). Returns
    (repo, shared_dir, builtin_path)."""
    repo = tmp_path / "repo"
    pkg_dir = repo / "gallery" / "morning-briefing"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "p-a9a74bf7.json").write_text(json.dumps({
        "pkg_id": "p-a9a74bf7",
        "pkg_version": "2026.06.12-2.3",
        "schema_version": 5,
        "name": "Morning Briefing",
        "objective": "deliver via openclaw message send",
        "build_spec": "## Delivery\nUse `openclaw message send`.",
        "files": [],
        "crons": [],
    }))
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"networkId": "pod-test"}))
    builtin = (
        shared / "gallery" / "builtin" / "p-a9a74bf7" / f"{_INITIAL_SPEC_VERSION}.json"
    )
    builtin.parent.mkdir(parents=True)
    # Stranded pre-#2695 content + no provenance → must be re-seeded.
    builtin.write_text(json.dumps({
        "spec_id": "p-a9a74bf7",
        "spec_version": _INITIAL_SPEC_VERSION,
        "objective": {"primary": "POST plain text to /api/message"},
    }))
    return repo, shared, builtin


def test_gallery_reseed_hook_reseeds_stale_builtin(tmp_path: Path):
    repo, shared, builtin = _build_reseed_layout(tmp_path)
    result = repo_puller.PullResult(success=True)
    repo_puller._run_gallery_reseed_hook(result, repo, shared)
    assert result.gallery_specs_reseeded == ["p-a9a74bf7"]
    assert result.gallery_reseed_error == ""
    spec = json.loads(builtin.read_text())
    assert spec["seeded_from_pkg_version"] == "2026.06.12-2.3"
    assert "openclaw message send" in spec["objective"]["primary"]
    # Reads the DEPLOY CHECKOUT's gallery (repo/gallery), so a step names it.
    assert any("re-seeded 1 builtin Spec" in s for s in result.steps)


def test_gallery_reseed_hook_is_idempotent(tmp_path: Path):
    repo, shared, _ = _build_reseed_layout(tmp_path)
    r1 = repo_puller.PullResult(success=True)
    repo_puller._run_gallery_reseed_hook(r1, repo, shared)
    assert r1.gallery_specs_reseeded == ["p-a9a74bf7"]
    r2 = repo_puller.PullResult(success=True)
    repo_puller._run_gallery_reseed_hook(r2, repo, shared)
    assert r2.gallery_specs_reseeded == []


def test_gallery_reseed_hook_never_raises(tmp_path: Path, monkeypatch):
    """A re-seed blow-up is captured on the result, never propagated — the
    pull (HEAD already advanced) must not fail on a healer glitch."""
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(
        "evolve_admin.applications.migrate_v7.reseed_builtin_specs", boom
    )
    result = repo_puller.PullResult(success=True)
    repo_puller._run_gallery_reseed_hook(result, tmp_path, tmp_path)
    assert "disk gone" in result.gallery_reseed_error
    assert any("FAIL builtin re-seed" in s for s in result.steps)


def test_pull_no_op_runs_gallery_reseed(tmp_path: Path):
    """End-to-end wiring: a no-op pull still runs the every-tick re-seed
    (a stale builtin is a standing condition, not gated on this pull's diff)."""
    repo, shared, builtin = _build_reseed_layout(tmp_path)
    sha = "a1b2c3d4e5f6789012345678901234567890abcd"
    fake = _make_git_runner({
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
    })
    with patch.object(repo_puller, "_git", fake):
        result = repo_puller.pull(repo=repo, shared_dir=shared)
    assert result.success is True
    assert result.head_before == result.head_after == sha
    assert result.gallery_specs_reseeded == ["p-a9a74bf7"]
    assert json.loads(builtin.read_text())["seeded_from_pkg_version"] == "2026.06.12-2.3"


def test_run_tick_maintenance_runs_gallery_reseed(tmp_path: Path):
    """Canary-mode ticks (which only call run_tick_maintenance, no code move)
    still heal stale builtins."""
    repo, shared, builtin = _build_reseed_layout(tmp_path)
    result = repo_puller.run_tick_maintenance(repo, shared_dir=shared)
    assert result.gallery_specs_reseeded == ["p-a9a74bf7"]
    assert json.loads(builtin.read_text())["seeded_from_pkg_version"] == "2026.06.12-2.3"


# ── Root-invocation guard: `sudo evolve-admin repo-pull` ──────────────────
#
# Root has no GitHub deploy key (it lives with the `evolve` service user
# the daemon runs as), so a root-euid pull ALWAYS fails with "Permission
# denied (publickey)". That is an invocation error, not a wedge — filing
# an incident for it produced two false-positive records on the mini
# (2026-07-01-001, 2026-07-31-001) and paged the alerts channel for both.

_PUBLICKEY_ERR = (
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists."
)


def _failing_auth_git():
    return _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", _PUBLICKEY_ERR),
    })


def test_tick_as_root_files_no_incident_for_auth_failure(tmp_path: Path):
    """THE regression: a root-euid invocation must not write an incident
    file, must not page, and must not emit a wedge Signal."""
    repo, incidents = _make_repo_layout(tmp_path)
    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    pages: list[tuple] = []
    signals: list[tuple] = []
    now = dt.datetime(2026, 7, 31, 4, 40, 41, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", _failing_auth_git()), \
         patch.object(repo_puller, "_effective_uid", lambda: 0), \
         patch.object(repo_puller, "observe_wedge_signal",
                      lambda **kw: signals.append(kw)), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(
            repo=repo, now=now, incidents_dir=incidents,
            notifier=lambda *a: (pages.append(a), (True, ""))[1],
        )

    assert result.pull.success is False
    assert list(incidents.iterdir()) == []      # nothing filed
    assert result.issue_path is None
    assert result.notified is False
    assert pages == []                          # alerts channel untouched
    assert signals == []                        # no wedge Signal either
    assert "cannot run as root" in result.invocation_error
    assert "sudo -H -u evolve" in result.invocation_error


def test_tick_as_root_still_files_incident_for_a_real_wedge(tmp_path: Path):
    """Root euid is not a blanket amnesty: a non-fast-forward under root is
    a genuine working-tree wedge and must still file."""
    repo, incidents = _make_repo_layout(tmp_path)
    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
    })
    now = dt.datetime(2026, 7, 31, 4, 40, 41, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(repo_puller, "_effective_uid", lambda: 0), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, now=now, incidents_dir=incidents,
                                  notifier=lambda *a: (True, ""))

    assert result.invocation_error == ""
    assert result.issue_path is not None and result.issue_path.exists()


def test_tick_as_evolve_files_incident_for_auth_failure(tmp_path: Path):
    """The other side of the distinction: the SAME publickey error from a
    non-root euid is the daemon's own key failing — a real wedge. It files,
    pages, and gets the deploy-key recipe (not the stash recipe)."""
    repo, incidents = _make_repo_layout(tmp_path)
    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    pages: list[tuple] = []
    now = dt.datetime(2026, 7, 31, 4, 40, 41, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", _failing_auth_git()), \
         patch.object(repo_puller, "_effective_uid", lambda: 501), \
         patch.object(repo_puller, "observe_wedge_signal", lambda **kw: None), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(
            repo=repo, now=now, incidents_dir=incidents,
            notifier=lambda *a: (pages.append(a), (True, ""))[1],
        )

    assert result.invocation_error == ""
    assert result.issue_path is not None
    assert len(pages) == 1
    body = result.issue_path.read_text()
    assert "could not AUTHENTICATE" in body
    assert "--setup-key" in body
    # The misleading default hypothesis must NOT be what an operator reads
    # for an auth failure — git never touched the working tree.
    assert "uncommitted local" not in body


def test_tick_log_shows_the_working_command_instead_of_a_filed_record(
    tmp_path: Path,
):
    """The operator's only feedback is the log line: it must name the
    command that works and say nothing was filed."""
    result = repo_puller.TickResult(
        pull=repo_puller.PullResult(
            success=False, error=f"pull --ff-only failed: {_PUBLICKEY_ERR}"),
        invocation_error=repo_puller.format_root_invocation_message(),
    )
    out = repo_puller.format_tick_for_log(result)
    assert "sudo -H -u evolve" in out
    assert "no incident was filed" in out
    assert "filed /" not in out


def test_enforce_evolve_invocation_reexecs_as_evolve_when_root(monkeypatch):
    """Layer 1: `sudo evolve-admin repo-pull` re-execs the whole command as
    the service account and exits with the child's status."""
    calls: list[tuple] = []
    exits: list[int] = []

    def fake_runner(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.delenv(repo_puller.REEXEC_GUARD_ENV, raising=False)
    monkeypatch.setattr(repo_puller._sys, "argv",
                        ["/venv/bin/evolve-admin", "repo-pull", "--quiet"])
    with patch.object(repo_puller, "_effective_uid", lambda: 0), \
         patch.object(repo_puller, "_evolve_user_exists", lambda: True):
        repo_puller.enforce_evolve_invocation(
            runner=fake_runner, exit_fn=exits.append)

    assert exits == [0]
    cmd, kw = calls[0]
    assert cmd[:4] == ["sudo", "-H", "-u", "evolve"]
    assert cmd[-2:] == ["repo-pull", "--quiet"]
    assert f"{repo_puller.REEXEC_GUARD_ENV}=1" in cmd
    # cwd must not be the operator's home — evolve can't traverse it and
    # python dies resolving sys.path[0] before main() runs.
    assert kw["cwd"] == "/tmp"


def test_enforce_evolve_invocation_passes_through_for_the_daemon():
    """The daemon already runs as evolve — no re-exec, no exit."""
    calls: list[tuple] = []
    with patch.object(repo_puller, "_effective_uid", lambda: 501):
        repo_puller.enforce_evolve_invocation(
            runner=lambda *a, **k: calls.append(a),
            exit_fn=lambda c: pytest.fail(f"exited {c}"))
    assert calls == []


def test_enforce_evolve_invocation_allows_root_for_setup_key():
    """`--setup-key` writes into evolve's ~/.ssh and MUST stay root."""
    with patch.object(repo_puller, "_effective_uid", lambda: 0):
        repo_puller.enforce_evolve_invocation(
            allow_root=True,
            runner=lambda *a, **k: pytest.fail("re-exec'd --setup-key"),
            exit_fn=lambda c: pytest.fail(f"exited {c}"))


def test_enforce_evolve_invocation_refuses_when_no_evolve_user(capsys, monkeypatch):
    """No service account (a dev box, a container): refuse with the
    actionable message and exit 2 — never fall through to a pull that
    would file a false incident."""
    exits: list[int] = []
    monkeypatch.delenv(repo_puller.REEXEC_GUARD_ENV, raising=False)
    with patch.object(repo_puller, "_effective_uid", lambda: 0), \
         patch.object(repo_puller, "_evolve_user_exists", lambda: False):
        repo_puller.enforce_evolve_invocation(
            runner=lambda *a, **k: pytest.fail("re-exec'd with no evolve user"),
            exit_fn=exits.append)
    assert exits == [2]
    err = capsys.readouterr().err
    assert "cannot run as root" in err
    assert "no `evolve` user on this host" in err


def test_enforce_evolve_invocation_does_not_reexec_twice(capsys, monkeypatch):
    """Recursion guard: a child that somehow lands as root again refuses
    instead of forking forever."""
    exits: list[int] = []
    monkeypatch.setenv(repo_puller.REEXEC_GUARD_ENV, "1")
    with patch.object(repo_puller, "_effective_uid", lambda: 0):
        repo_puller.enforce_evolve_invocation(
            runner=lambda *a, **k: pytest.fail("re-exec'd a second time"),
            exit_fn=exits.append)
    assert exits == [2]
    assert "still running as root" in capsys.readouterr().err


@pytest.mark.parametrize("error,expected", [
    ("git@github.com: Permission denied (publickey).", True),
    ("fatal: Could not read from remote repository.", True),
    ("remote: Authentication failed for 'https://github.com/x'", True),
    ("fatal: Not possible to fast-forward, aborting.", False),
    ("error: Your local changes would be overwritten by merge", False),
    ("", False),
    # Connectivity, not auth — ssh never reached the point of offering a
    # key, so this is a real pod problem and must keep filing whoever ran
    # it. The trailing "Could not read from remote repository" line is
    # shared with the publickey shape, so the connectivity markers win.
    ("ssh: connect to host github.com port 22: Undefined error: 0\n"
     "fatal: Could not read from remote repository.", False),
    ("ssh: Could not resolve hostname github.com", False),
])
def test_is_remote_auth_failure_classification(error: str, expected: bool):
    assert repo_puller.is_remote_auth_failure(error) is expected


def test_tick_as_root_files_incident_when_the_pod_cannot_reach_github(
    tmp_path: Path,
):
    """Root euid does not suppress a CONNECTIVITY failure: the pull never
    got as far as authenticating, so the deploy box has a real problem
    (this is the 2026-07-10-001 shape) and it must still file."""
    repo, incidents = _make_repo_layout(tmp_path)
    real_exists = Path.exists

    def fake_exists(self):
        return True if self == repo else real_exists(self)

    fake = _make_git_runner({
        "rev-parse": (0, "a" * 40, ""),
        "pull": (1, "", "ssh: connect to host github.com port 22: "
                        "Undefined error: 0\n"
                        "fatal: Could not read from remote repository."),
    })
    now = dt.datetime(2026, 7, 10, 2, 32, 0, tzinfo=dt.timezone.utc)
    with patch.object(repo_puller, "_git", fake), \
         patch.object(repo_puller, "_effective_uid", lambda: 0), \
         patch.object(repo_puller, "observe_wedge_signal", lambda **kw: None), \
         patch.object(Path, "exists", fake_exists):
        result = repo_puller.tick(repo=repo, now=now, incidents_dir=incidents,
                                  notifier=lambda *a: (True, ""))

    assert result.invocation_error == ""
    assert result.issue_path is not None and result.issue_path.exists()


def test_resolve_admin_bin_prefers_the_binary_the_operator_invoked(tmp_path: Path):
    """Re-running exactly what was typed is the only resolution that can't
    swap in a different install of evolve-admin."""
    invoked = tmp_path / "venv" / "bin" / "evolve-admin"
    invoked.parent.mkdir(parents=True)
    invoked.write_text("#!/bin/sh\n")
    decoy = tmp_path / "usr-local" / "evolve-admin"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("#!/bin/sh\n")
    with patch.object(repo_puller._sys, "executable", str(decoy.parent / "python3")):
        assert repo_puller._resolve_admin_bin(str(invoked)) == str(invoked)


def test_resolve_admin_bin_does_not_resolve_the_venv_interpreter_symlink(
    tmp_path: Path,
):
    """A venv's bin/python3 is a symlink to the system interpreter. Resolving
    it walks OUT of the venv, so the sibling lookup would always miss and fall
    through to `which` — which on the mini picked /usr/local/bin/evolve-admin
    instead of the venv script the operator ran."""
    system_bin = tmp_path / "brew" / "bin"
    system_bin.mkdir(parents=True)
    (system_bin / "python3.14").write_text("#!/bin/sh\n")
    venv_bin = tmp_path / "evolve-venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(system_bin / "python3.14")
    (venv_bin / "evolve-admin").write_text("#!/bin/sh\n")

    with patch.object(repo_puller._sys, "executable", str(venv_bin / "python3")):
        # Bare name (nothing to stat directly) → the venv sibling, NOT a
        # PATH lookup.
        assert repo_puller._resolve_admin_bin("evolve-admin") == str(
            venv_bin / "evolve-admin")
