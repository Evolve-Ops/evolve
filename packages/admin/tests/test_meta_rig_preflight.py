"""Unit tests for tools/meta_rig_preflight — D-TP1: exercise every dispatch-tick
precondition instead of asserting one.

Each check gets its own red-line pin (checkout / main CI / token scopes / lane health),
`rig_preflight()` gets the "unknown counts as red" pin, and `four_way_corroboration` gets
the full 16-case matrix (only all-four-absent corroborates a genuine stall). No network:
every `gh`/`git` call here goes through an injected `runner`, never a real subprocess.
"""

from __future__ import annotations

import itertools
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[3] / "tools"
sys.path.insert(0, str(_TOOLS))

import meta_rig_preflight as mrp  # noqa: E402
import meta_dispatch_headless as mdh  # noqa: E402


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_runner(table):
    """A runner that answers by matching each call's argv PREFIX against `table`
    (an ordered list of (prefix_tuple, FakeProc)); the first match wins."""
    def run(argv, cwd=None):
        for prefix, proc in table:
            if tuple(argv[:len(prefix)]) == tuple(prefix):
                return proc
        return FakeProc(9, "", "unexpected call: %r" % (argv,))
    return run


# ── checkout_state ───────────────────────────────────────────────────────────


def test_checkout_state_ok_when_on_base_clean_and_current():
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count"), FakeProc(0, "0\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is True
    assert line is None


def test_checkout_state_names_the_wrong_branch_and_the_remedy():
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "feature-x\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is False
    assert "feature-x" in line and "not 'main'" in line
    assert "sync" in line


def test_checkout_state_names_dirty():
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, " M some/file.py\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is False
    assert "dirty" in line
    assert "some/file.py" in line


def test_checkout_state_ignores_the_lanes_own_staged_moves():
    """The dispatcher's `queued/ -> inflight/ -> done/` moves sit staged between `launch`
    and `land`; a preflight that reds on them stops the tick that lands them (2026-09-23:
    13 ticks). Same scope rule as `meta-dispatch-move sync`."""
    porcelain = (
        " M internal/dispatch/CARDS.md\n"
        "R  internal/dispatch/inflight/a.md -> internal/dispatch/done/a.md\n"
        "A  internal/dispatch/inflight/b.md\n"
        "D  internal/dispatch/inflight/c.md\n"
        "?? internal/dispatch/repairs/done/\n"
    )
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, porcelain)),
        (("git", "rev-list", "--count"), FakeProc(0, "0\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is True, line


def test_checkout_state_ignores_untracked_files_like_sync_does():
    porcelain = (
        "?? internal/_pm-landing/finding-x.json.landed-by-pm-land\n"
        "?? scratch/notes.txt\n"
        "R  internal/dispatch/inflight/a.md -> internal/dispatch/done/a.md\n"
    )
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, porcelain)),
        (("git", "rev-list", "--count"), FakeProc(0, "0\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is True, line


def test_checkout_state_still_reds_on_a_human_edit_beside_lane_moves():
    porcelain = (
        "R  internal/dispatch/inflight/a.md -> internal/dispatch/done/a.md\n"
        " M packages/admin/evolve_admin/deploy.py\n"
    )
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, porcelain)),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is False
    assert "deploy.py" in line and "internal/dispatch" not in line


def test_checkout_state_is_green_when_behind_because_sync_is_the_next_step():
    """2026-09-24: 18 consecutive runner ticks stopped at step 0a on "3 commit(s) behind
    origin/main — run sync" while step 0b IS sync. Behind is what the tick fixes next, so
    it must not be what stops the tick (the two-dirs rule, applied to the checkout)."""
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count", "origin/main..HEAD"), FakeProc(0, "0\n")),
        (("git", "rev-list", "--count", "HEAD..origin/main"), FakeProc(0, "7\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is True, line


def test_checkout_state_reds_when_ahead_because_a_fast_forward_cannot_run():
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count", "origin/main..HEAD"), FakeProc(0, "2\n")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is False
    assert "2 commit" in line and "AHEAD" in line and "origin/main" in line


def test_checkout_state_never_counts_behind_at_all():
    """The behind count is not even asked for: a runner that would fail on
    `HEAD..origin/main` never sees the call."""
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count", "origin/main..HEAD"), FakeProc(0, "0\n")),
        (("git", "rev-list", "--count", "HEAD..origin/main"), FakeProc(1, "", "boom")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is True, line


def test_checkout_state_unknown_when_git_cannot_answer():
    run = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(1, "", "not a repo")),
    ])
    ok, line = mrp.checkout_state("/repo", runner=run)
    assert ok is False
    assert "unknown" in line


def test_checkout_state_unknown_with_no_repo():
    ok, line = mrp.checkout_state(None)
    assert ok is False
    assert "unknown" in line


# ── main_ci_state ────────────────────────────────────────────────────────────
#
# Fixture shapes trimmed from real `gh api` responses captured 2026-09-21: run
# 34872064573 (a real push-to-main `ci` failure whose only failed job is the quarantined
# Linux e2e) and run 35049939899 (a real failure whose failed jobs — "Admin suite shard
# 2/4", "Full admin test suite (quarantined baseline)" — are NOT quarantined).


def _runs_payload(rows):
    return json.dumps({"total_count": len(rows), "workflow_runs": rows})


def _jobs_payload(jobs):
    return json.dumps({"total_count": len(jobs), "jobs": jobs})


def _ci_runner(runs, jobs=None):
    """A runner for `main_ci_state`'s (up to) two `gh api` calls: the push-event run
    list, then — only when the top `ci` run's conclusion is not green — that run's
    jobs."""
    def run(argv, cwd=None):
        path = argv[-1]
        if "/jobs" in path:
            return FakeProc(0, _jobs_payload(jobs or []))
        return FakeProc(0, _runs_payload(runs))
    return run


def test_main_ci_green_when_latest_completed_run_succeeded():
    run = _ci_runner([{"id": 1, "name": "ci", "status": "completed", "conclusion": "success",
                       "head_sha": "a" * 40, "updated_at": "2026-09-19T09:24:27Z"}])
    ok, line = mrp.main_ci_state("/repo", runner=run)
    assert ok is True and line is None


def test_main_ci_ignores_other_workflows_and_queries_push_events(tmp_path):
    """Both measured live 2026-09-21: (a) `secret-history-scan`/`publish-public` also
    complete on the SAME push as `ci` — a failure in either must not redden the rig
    (finding 2); (b) `gh run list --branch main` (the old call) returned a run from
    2026-09-07 — two weeks stale — while `event=push&branch=main` did not, so the fix
    also pins the call SHAPE, not only the workflow-name filter."""
    calls = []

    def run(argv, cwd=None):
        calls.append(argv)
        return FakeProc(0, _runs_payload([
            {"id": 2, "name": "secret-history-scan", "status": "completed",
             "conclusion": "failure", "head_sha": "b" * 40, "updated_at": "2026-09-19T09:20:00Z"},
            {"id": 1, "name": "ci", "status": "completed", "conclusion": "success",
             "head_sha": "a" * 40, "updated_at": "2026-09-19T09:24:27Z"},
        ]))

    ok, line = mrp.main_ci_state("/repo", branch="main", runner=run)
    assert ok is True and line is None
    assert calls[0][:2] == ["gh", "api"] and "gh run list" not in " ".join(calls[0])
    assert "event=push" in calls[0][-1] and "branch=main" in calls[0][-1]


def test_main_ci_green_when_the_only_failing_job_is_quarantined(tmp_path):
    quarantine = tmp_path / "quarantine.txt"
    quarantine.write_text(
        "Linux e2e (Ubuntu bot deploy/run/admin)\tplatform-owner\t2099-01-01\t# test\n",
        encoding="utf-8")
    run = _ci_runner(
        [{"id": 34872064573, "name": "ci", "status": "completed", "conclusion": "failure",
          "head_sha": "a14a1f50ab19" + "0" * 28, "updated_at": "2026-09-14T17:08:07Z"}],
        jobs=[{"name": "Linux e2e (Ubuntu bot deploy/run/admin)", "conclusion": "failure"},
              {"name": "Public manifest cut-line guard", "conclusion": "success"}])
    ok, line = mrp.main_ci_state("/repo", runner=run, quarantine_path=quarantine,
                                 today=date(2026, 9, 21))
    assert ok is True and line is None


def test_main_ci_red_names_the_failing_job_not_the_workflow(tmp_path):
    quarantine = tmp_path / "quarantine.txt"
    quarantine.write_text("", encoding="utf-8")
    run = _ci_runner(
        [{"id": 35049939899, "name": "ci", "status": "completed", "conclusion": "failure",
          "head_sha": "3c3fdaccf5dd" + "0" * 28, "updated_at": "2026-09-16T03:03:59Z"}],
        jobs=[{"name": "Admin suite shard 2/4", "conclusion": "failure"},
              {"name": "Full admin test suite (quarantined baseline)", "conclusion": "failure"},
              {"name": "Public manifest cut-line guard", "conclusion": "success"}])
    ok, line = mrp.main_ci_state("/repo", runner=run, quarantine_path=quarantine,
                                 today=date(2026, 9, 21))
    assert ok is False
    assert "main is red" in line and "3c3fdaccf5dd" in line
    assert "Admin suite shard 2/4" in line
    assert "Full admin test suite (quarantined baseline)" in line


def test_main_ci_red_when_quarantine_entry_expired(tmp_path):
    quarantine = tmp_path / "quarantine.txt"
    quarantine.write_text(
        "Linux e2e (Ubuntu bot deploy/run/admin)\tplatform-owner\t2020-01-01\t# stale\n",
        encoding="utf-8")
    run = _ci_runner(
        [{"id": 1, "name": "ci", "status": "completed", "conclusion": "failure",
          "head_sha": "a" * 40, "updated_at": "2026-09-14T17:08:07Z"}],
        jobs=[{"name": "Linux e2e (Ubuntu bot deploy/run/admin)", "conclusion": "failure"}])
    ok, line = mrp.main_ci_state("/repo", runner=run, quarantine_path=quarantine,
                                 today=date(2026, 9, 21))
    assert ok is False and "Linux e2e" in line


def test_main_ci_unknown_when_gh_fails():
    run = lambda argv, cwd=None: FakeProc(1, "", "gh: not authenticated")
    ok, line = mrp.main_ci_state("/repo", runner=run)
    assert ok is False
    assert "unknown" in line


def test_main_ci_unknown_when_no_completed_ci_run_found():
    run = _ci_runner([{"id": 1, "name": "publish-public", "status": "completed",
                       "conclusion": "success", "head_sha": "a" * 40,
                       "updated_at": "2026-09-19T09:24:27Z"}])
    ok, line = mrp.main_ci_state("/repo", runner=run)
    assert ok is False
    assert "unknown" in line


def test_main_ci_unknown_with_no_repo():
    ok, line = mrp.main_ci_state(None)
    assert ok is False and "unknown" in line


# ── token_access ─────────────────────────────────────────────────────────────
#
# Fixtures trimmed from real `gh api -i <path>` output captured 2026-09-21 (PM-RULINGS
# 2026-09-19: "a fixture for an external tool's output is captured from the tool, never
# written from the requirement") — a 200 and a 404, pinning the non-200-reads-as-a-named-
# red shape.

_GH_API_200 = "HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{}"
_GH_API_404 = ('HTTP/2.0 404 Not Found\r\nContent-Type: application/json; charset=utf-8'
              '\r\n\r\n{\n  "message": "Not Found",\n  "documentation_url": '
              '"https://docs.github.com/rest"\n}')


def test_token_access_ok_when_both_reads_return_200():
    run = lambda argv, cwd=None: FakeProc(0, _GH_API_200)
    ok, lines = mrp.token_access(runner=run)
    assert ok is True and lines == []


def test_token_access_names_a_non_200_read_with_its_status():
    def run(argv, cwd=None):
        if "workflows" in argv[-1]:
            return FakeProc(1, _GH_API_404)
        return FakeProc(0, _GH_API_200)
    ok, lines = mrp.token_access(runner=run)
    assert ok is False
    assert len(lines) == 1
    assert "404" in lines[0] and "workflow" in lines[0]
    # the OTHER read (200) must not also be reported
    assert not any("per_page=1" in l for l in lines)


def test_token_access_unknown_when_gh_unreachable():
    run = lambda argv, cwd=None: FakeProc(127, "", "gh: command not found")
    ok, lines = mrp.token_access(runner=run)
    assert ok is False
    assert len(lines) == 2
    assert all("unknown" in l for l in lines)


def test_token_access_never_reads_check_runs():
    """RULINGS 2026-09-16: `/check-runs` 403s even with the D-TP6 grant — the repair
    reads `/actions/*` instead, and this pins that neither read ever names it."""
    for path, _ in mrp.TOKEN_READS:
        assert "check-runs" not in path


# ── lane_state ───────────────────────────────────────────────────────────────


def _write_entry(root, state, ident, extra_fm=""):
    d = root / state
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.md" % ident)).write_text(
        "---\nid: %s\n%s---\nsome body text\n" % (ident, extra_fm), encoding="utf-8")


def test_lane_state_ok_on_a_clean_lane(tmp_path):
    _write_entry(tmp_path, "queued", "a")
    _write_entry(tmp_path, "inflight", "b")
    ok, lines = mrp.lane_state(tmp_path)
    assert ok is True and lines == []


def test_lane_state_flags_an_id_in_two_dirs(tmp_path):
    _write_entry(tmp_path, "queued", "dup")
    _write_entry(tmp_path, "inflight", "dup")
    ok, lines = mrp.lane_state(tmp_path)
    assert ok is False
    assert any("dup" in l and "2 dir" in l for l in lines)


def test_lane_state_flags_invalid_front_matter(tmp_path):
    d = tmp_path / "queued"
    d.mkdir(parents=True)
    (d / "broken.md").write_text("no front matter here at all", encoding="utf-8")
    ok, lines = mrp.lane_state(tmp_path)
    assert ok is False
    assert any("INVALID" in l for l in lines)


def test_lane_state_flags_a_stale_lock(tmp_path):
    lock = tmp_path / "dispatch.lock"
    lock.write_text("x", encoding="utf-8")
    import os
    os.utime(lock, (0, 0))
    ok, lines = mrp.lane_state(tmp_path, now_ts=10_000_000)
    assert ok is False
    assert any("stale" in l for l in lines)


def test_lane_state_a_young_lock_is_fine(tmp_path):
    lock = tmp_path / "dispatch.lock"
    lock.write_text("x", encoding="utf-8")
    ok, lines = mrp.lane_state(tmp_path, now_ts=lock.stat().st_mtime + 5)
    assert ok is True and lines == []


# ── rig_preflight aggregate ────────────────────────────────────────────────


def _all_green_runners():
    git = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count"), FakeProc(0, "0\n")),
    ])
    def gh(argv, cwd=None):
        if argv[:2] != ["gh", "api"]:
            return FakeProc(9)
        path = argv[-1]
        if path in dict(mrp.TOKEN_READS):
            return FakeProc(0, _GH_API_200)
        if "/jobs" in path:
            return FakeProc(0, _jobs_payload([]))
        return FakeProc(0, _runs_payload(
            [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success",
              "head_sha": "a" * 40, "updated_at": "2026-09-19T09:24:27Z"}]))
    return git, gh


def test_rig_preflight_all_green_is_ok(tmp_path):
    """Default `probe_login=False`: login is not evaluated at all, so an all-green
    checkout/CI/token/lane tick reads `ok` with no login runner ever invoked."""
    git, gh = _all_green_runners()
    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh)
    assert result.ok is True
    assert result.lines == ()
    assert result.may_reconcile is True and result.may_launch is True
    assert "not probed this tick" in result.login_note


def test_rig_preflight_an_id_in_two_dirs_blocks_launch_but_not_reconcile(tmp_path):
    """A merged chip leaves its id in inflight/ and done/ until the tick's own step 1
    runs `done <id>`; that is what reconciling FIXES, so it must not forbid reconciling
    (2026-09-23: five such ids held the cap for 13 ticks). INVALID entries still block both."""
    git, gh = _all_green_runners()
    _write_entry(tmp_path, "inflight", "merged-chip")
    _write_entry(tmp_path, "done", "merged-chip")
    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh)
    assert result.may_launch is False
    assert result.may_reconcile is True
    assert any("merged-chip" in line and "2 dir" in line for line in result.lines)


def test_rig_preflight_an_invalid_entry_blocks_reconcile_too(tmp_path):
    git, gh = _all_green_runners()
    (tmp_path / "done").mkdir(parents=True, exist_ok=True)
    (tmp_path / "done" / "broken.md").write_text("---\nid: broken\nbody_sha256: sha256:0000\n---\nbody\n")
    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh)
    assert result.may_reconcile is False and result.may_launch is False


def test_rig_preflight_probe_login_ok_caches_the_verdict(tmp_path):
    git, gh = _all_green_runners()

    def login_runner(argv, cwd=None):
        if argv[:2] == ["claude", "--bg"]:
            return FakeProc(0, "backgrounded · 9988776\n")
        if argv[:2] == ["claude", "agents"]:
            return FakeProc(0, json.dumps(
                [{"id": "9988776", "status": "completed", "result": "PROBE_OK"}]))
        return FakeProc(9)

    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh,
                               login_runner=login_runner, probe_login=True, now_ts=1000.0)
    assert result.ok is True and result.may_launch is True
    assert result.login_note == "login: probed"
    assert result.login_cache_update == {"state": mdh.LOGIN_OK, "at": mrp._iso_utc(1000.0)}


def test_rig_preflight_probe_login_expired_blocks_launch_only_and_is_first(tmp_path):
    git, gh = _all_green_runners()

    def login_runner(argv, cwd=None):
        if argv[:2] == ["claude", "--bg"]:
            return FakeProc(0, "backgrounded · 9988776\n")
        if argv[:2] == ["claude", "agents"]:
            return FakeProc(0, json.dumps(
                [{"id": "9988776", "status": "idle", "state": "blocked"}]))
        return FakeProc(9)

    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh,
                               login_runner=login_runner, probe_login=True)
    assert result.ok is False
    assert result.may_reconcile is True   # login never touches reconcile
    assert result.may_launch is False
    assert result.lines[0].startswith("rig: login expired")
    assert result.login_cache_update is None   # never cache a bad verdict


def test_rig_preflight_login_cache_fresh_vs_expired(tmp_path):
    """A stale cache must NOT block `may_launch` — the deadlock this avoids
    (`rig_preflight`'s own docstring): step 0a never probes, so if a stale/absent cache
    blocked it there, no tick could ever reach the one place (`probe_login=True`, step 4)
    that refreshes the cache. A fresh cache reports its age."""
    git, gh = _all_green_runners()
    cached = {"state": mdh.LOGIN_OK, "at": mrp._iso_utc(0.0)}

    fresh = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh,
                              cached_login=cached, now_ts=7993.0)  # 2h13m old
    assert fresh.login_note == "login: ok (cached 2h13m)"

    expired = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh,
                                cached_login=cached, now_ts=mrp.LOGIN_CACHE_TTL_S + 1)
    assert expired.ok is True and expired.may_launch is True
    assert "not probed this tick" in expired.login_note and "expired" in expired.login_note


def test_rig_preflight_unknown_check_still_blocks(tmp_path):
    """D-TP1's guardrail: unknown counts exactly as red for launching."""
    git = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(1, "", "no repo here")),
    ])
    _, gh = _all_green_runners()
    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh)
    assert result.ok is False
    assert result.may_reconcile is False and result.may_launch is False
    assert any("unknown" in l for l in result.lines)


def test_rig_preflight_collects_every_red_line_not_just_the_first(tmp_path):
    git = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "feature\n")),
    ])
    gh_all_red = lambda argv, cwd=None: FakeProc(1, "", "gh unavailable")
    _write_entry(tmp_path, "queued", "dup")
    _write_entry(tmp_path, "inflight", "dup")
    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh_all_red)
    assert result.ok is False
    # checkout + main-ci + token(x2) + lane = 5 lines
    assert len(result.lines) >= 4
    assert any("not 'main'" in l for l in result.lines)
    assert any("lane holds id" in l for l in result.lines)
    assert result.may_reconcile is False and result.may_launch is False


def test_rig_preflight_main_ci_red_may_reconcile_but_not_launch(tmp_path):
    """The hold-fix's headline contract (`internal/dispatch/reviews/pr-4351.md`
    finding 2b): a red main must not additionally stop the fast-forward, the repair
    queue, or the `inflight/` reconcile."""
    git = _git_runner([
        (("git", "rev-parse", "--abbrev-ref"), FakeProc(0, "main\n")),
        (("git", "status", "--porcelain"), FakeProc(0, "")),
        (("git", "rev-list", "--count"), FakeProc(0, "0\n")),
    ])

    def gh(argv, cwd=None):
        if argv[:2] != ["gh", "api"]:
            return FakeProc(9)
        path = argv[-1]
        if path in dict(mrp.TOKEN_READS):
            return FakeProc(0, _GH_API_200)
        if "/jobs" in path:
            return FakeProc(0, _jobs_payload(
                [{"name": "Admin suite shard 2/4", "conclusion": "failure"}]))
        return FakeProc(0, _runs_payload(
            [{"id": 1, "name": "ci", "status": "completed", "conclusion": "failure",
              "head_sha": "a" * 40, "updated_at": "2026-09-19T09:24:27Z"}]))

    result = mrp.rig_preflight("/repo", tmp_path, git_runner=git, gh_runner=gh)
    assert result.may_reconcile is True
    assert result.may_launch is False
    assert any("Admin suite shard 2/4" in l for l in result.lines)




# ── four_way_corroboration: the 16-case matrix ────────────────────────────────


def _corrob_runners(*, branch, ref, pr, worktree_evidence):
    entry = {"id": "chip-x", "branch": ("claude/meta-chip-x" if branch else None),
            "started": "2026-09-01T00:00:00Z"}

    def git_runner(argv, cwd=None):
        if argv[:2] == ["git", "for-each-ref"]:
            # The remote-tracking ref is the only one asked about; a local head is the
            # launcher's own footprint and is never queried.
            assert argv[-1].startswith("refs/remotes/origin/"), argv
            return FakeProc(0, "deadbeef\n" if ref else "")
        return FakeProc(9)

    def gh_runner(argv, cwd=None):
        if argv[:3] == ["gh", "pr", "list"]:
            rows = ([{"number": 1, "body": "fixes chip-x", "headRefName": "claude/meta-chip-x"}]
                    if pr else [])
            return FakeProc(0, json.dumps(rows))
        return FakeProc(9)

    def mtime_fn(path):
        # started epoch for 2026-09-01T00:00:00Z, plus slack, plus 1s if evidence wanted
        started = mdh._epoch(entry["started"])
        return (started + mrp.WORKTREE_CHECKOUT_SLACK_S + 1) if worktree_evidence else None

    return entry, git_runner, gh_runner, mtime_fn


@pytest.mark.parametrize("branch,ref,pr,worktree", list(itertools.product([False, True],
                                                                          repeat=4)))
def test_four_way_corroboration_matrix(branch, ref, pr, worktree, tmp_path):
    entry, git_runner, gh_runner, mtime_fn = _corrob_runners(
        branch=branch, ref=ref, pr=pr, worktree_evidence=worktree)
    all_absent, findings, detail = mrp.four_way_corroboration(
        entry, tmp_path, git_runner=git_runner, gh_runner=gh_runner, mtime_fn=mtime_fn)
    any_present = branch or ref or pr or worktree
    assert all_absent == (not any_present)
    assert findings == {"branch": branch, "ref": ref, "pr": pr, "worktree": worktree}
    if any_present:
        assert "corroborated by" in detail
    else:
        assert "no branch" in detail


def test_four_way_corroboration_fails_toward_found_with_no_repo():
    entry = {"id": "chip-x", "branch": None, "started": "2026-09-01T00:00:00Z"}
    all_absent, findings, detail = mrp.four_way_corroboration(entry, None)
    assert all_absent is False
    assert findings["ref"] is True and findings["pr"] is True and findings["worktree"] is True


def test_four_way_corroboration_fails_toward_found_when_git_errors(tmp_path):
    entry = {"id": "chip-x", "branch": None, "started": "2026-09-01T00:00:00Z"}
    git_runner = lambda argv, cwd=None: FakeProc(1, "", "git broken")
    gh_runner = lambda argv, cwd=None: FakeProc(1, "", "gh broken")
    all_absent, findings, detail = mrp.four_way_corroboration(
        entry, tmp_path, git_runner=git_runner, gh_runner=gh_runner)
    assert all_absent is False
    assert findings["ref"] is True and findings["pr"] is True


def test_four_way_corroboration_ignores_the_lanes_own_prs(tmp_path):
    """2026-09-24: `lane/state`'s body names every in-flight id, and a `pm/*` bookkeeping
    PR discusses chips by id — neither is evidence the chip did anything. A dead chip read
    as "corroborated by: pr" for 24 h because of the standing lane PR."""
    entry = {"id": "chip-x", "branch": None, "started": "2026-09-01T00:00:00Z"}
    rows = [{"number": 4391, "body": "lane: bind chip-x -> #1", "headRefName": "lane/state"},
            {"number": 4446, "body": "rulings: chip-x is dead", "headRefName": "pm/state"}]

    def git_runner(argv, cwd=None):
        return FakeProc(0, "")

    def gh_runner(argv, cwd=None):
        return FakeProc(0, json.dumps(rows))
    all_absent, findings, detail = mrp.four_way_corroboration(
        entry, tmp_path, git_runner=git_runner, gh_runner=gh_runner, mtime_fn=lambda p: None)
    assert findings["pr"] is False and findings["ref"] is False
    assert all_absent is True, detail


def test_four_way_corroboration_reads_the_pushed_ref_not_the_local_head(tmp_path):
    """The launcher creates the local `claude/meta-<id>` branch when it cuts the worktree;
    only the remote-tracking ref means a push happened."""
    entry = {"id": "chip-x", "branch": None, "started": "2026-09-01T00:00:00Z"}
    asked = []

    def git_runner(argv, cwd=None):
        asked.append(argv[-1])
        return FakeProc(0, "")

    def gh_runner(argv, cwd=None):
        return FakeProc(0, "[]")
    mrp.four_way_corroboration(entry, tmp_path, git_runner=git_runner,
                               gh_runner=gh_runner, mtime_fn=lambda p: None)
    assert asked == ["refs/remotes/origin/claude/meta-chip-x"]


# ── find_existing_successor ───────────────────────────────────────────────


def test_find_existing_successor_locates_a_supersedes_entry(tmp_path):
    _write_entry(tmp_path, "queued", "chip-x-2", extra_fm="supersedes: chip-x\n")
    found = mrp.find_existing_successor(tmp_path, "chip-x")
    assert found is not None and found.name == "chip-x-2.md"


def test_find_existing_successor_none_when_absent(tmp_path):
    _write_entry(tmp_path, "queued", "chip-x")
    assert mrp.find_existing_successor(tmp_path, "chip-x") is None
