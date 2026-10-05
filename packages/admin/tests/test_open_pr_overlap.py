"""Tests for tools/open_pr_overlap.py — the preflight open-PR overlap warning.

The 2026-09-22 collision these lock: a ``spawn_task`` chip and an app session's
open PR edited the same four files in parallel, because the lane's ``touches:``
hold (D-PM13) only sees briefs in ``inflight/`` and neither party had one. The
check therefore has to work from real changed-file lists and from ``gh``, and it
has to say something useful when ``gh`` cannot answer.

Placeholder-only data per docs/PLACEHOLDER_NAMING.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Lives in packages/admin/tests/ rather than next to the module, because THIS is
# the tree CI runs: the admin suite. A tools/test_*.py file is executed by
# nothing but the publish gate's two named suites, so a test placed there would
# assert and never run.
_TOOLS = Path(__file__).resolve().parents[3] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import open_pr_overlap as ovl  # noqa: E402


def _pr(number, paths, *, branch=None, title="some pull request"):
    return {
        "number": number,
        "title": title,
        "headRefName": branch or f"claude/pr-{number}",
        "files": [{"path": p} for p in paths],
    }


# ── The collision this exists for ────────────────────────────────────────


def test_reports_the_live_collision_shape():
    """The real 2026-09-22 pair: a chip's branch and an app session's open PR
    over one resolver, one monitor, one SPA file and one spec."""
    mine = [
        "packages/admin/evolve_admin/roster_resolver.py",
        "packages/analyzer/roster_coherence_monitor.py",
        "packages/admin/evolve_admin/web/static/js/pages/users.js",
        "internal/spec-users-meta-2026-06-15.md",
    ]
    theirs = _pr(4413, mine[:3] + ["packages/admin/tests/test_roster_resolver.py"],
                 title="the group/channel allowlist is an admission gate")
    got = ovl.find_overlaps(mine, [theirs], exclude_branch="claude/mine")
    assert len(got) == 1
    assert got[0].number == 4413
    assert got[0].paths == sorted(mine[:3])


def test_no_overlap_is_silent():
    mine = ["packages/admin/evolve_admin/deploy.py"]
    other = _pr(1, ["docs/help/installation.md"])
    assert ovl.find_overlaps(mine, [other]) == []
    assert ovl.render(ovl.Scan(overlaps=[], error=None, checked=1)) == []


# ── Never report yourself ────────────────────────────────────────────────


def test_excludes_own_pr_by_branch():
    """Updating your own PR is not a collision with yourself — reporting it
    would fire on the single most common invocation and train the reader to
    ignore the check."""
    mine = ["packages/admin/evolve_admin/deploy.py"]
    own = _pr(10, mine, branch="claude/my-branch")
    assert ovl.find_overlaps(mine, [own], exclude_branch="claude/my-branch") == []
    assert ovl.find_overlaps(mine, [own], exclude_number=10) == []
    # …but it IS reported when neither exclusion matches (detached HEAD):
    assert [o.number for o in ovl.find_overlaps(mine, [own])] == [10]


# ── Noise control ────────────────────────────────────────────────────────


def test_lane_bookkeeping_paths_are_not_overlap():
    """Nearly every lane PR moves a brief and CARDS.md; a check that fired on
    those would be ignored within a day. The lane has its own duplicate
    detector (D-TP5) for lane rows."""
    mine = [
        "internal/dispatch/CARDS.md",
        "internal/dispatch/queued/some-brief.md",
        "internal/meta-state/substrate.json",
    ]
    other = _pr(2, mine)
    assert ovl.find_overlaps(mine, [other]) == []


def test_lane_path_does_not_mask_a_real_overlap():
    """A PR that moves lane files AND edits real code still reports the code."""
    mine = ["internal/dispatch/CARDS.md", "packages/admin/evolve_admin/deploy.py"]
    other = _pr(3, ["internal/dispatch/CARDS.md",
                    "packages/admin/evolve_admin/deploy.py"])
    got = ovl.find_overlaps(mine, [other])
    assert [o.paths for o in got] == [["packages/admin/evolve_admin/deploy.py"]]


def test_most_entangled_pr_is_reported_first():
    mine = ["a.py", "b.py", "c.py"]
    prs = [_pr(7, ["c.py"]), _pr(8, ["a.py", "b.py", "c.py"])]
    assert [o.number for o in ovl.find_overlaps(mine, prs)] == [8, 7]


# ── "Could not check" must never read as "clean" ─────────────────────────


def test_unavailable_gh_renders_a_named_line():
    """A check that did not run must not look like a check that found nothing —
    the skip-green shape. The reason is named so the reader can act on it."""
    lines = ovl.render(ovl.Scan(overlaps=[], error="gh not installed", checked=0))
    assert len(lines) == 1
    assert "not checked" in lines[0] and "gh not installed" in lines[0]


def test_fetch_errors_are_strings_never_exceptions(monkeypatch):
    """Every gh failure mode comes back as an error string: this runs inside a
    push-time helper and must not crash it."""
    import subprocess

    def _boom(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(subprocess, "run", _boom)
    prs, err = ovl.fetch_open_prs()
    assert prs == [] and err == "gh not installed"

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=ovl.TIMEOUT_S)
    monkeypatch.setattr(subprocess, "run", _timeout)
    _prs, err = ovl.fetch_open_prs()
    assert "timed out" in err

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "gh: not logged in to github.com\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    _prs, err = ovl.fetch_open_prs()
    assert err == "gh: not logged in to github.com"

    class _Garbage:
        returncode = 0
        stdout = "not json"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Garbage())
    _prs, err = ovl.fetch_open_prs()
    assert "unparseable" in err


def test_scan_does_not_call_gh_when_nothing_relevant_changed(monkeypatch):
    """An empty or lane-only diff has nothing to compare, so the check costs no
    network call at all — it runs on every push and must stay cheap."""
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("gh must not be called")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert ovl.scan([]).overlaps == []
    assert ovl.scan(["internal/dispatch/CARDS.md"]).error is None


def test_one_gh_call_for_every_pr(monkeypatch):
    """One `gh pr list --json files` request, never one per PR — the fan-out
    form is the scaling trap this repo has hit before."""
    import subprocess

    calls = []

    class _Ok:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def _record(argv, **k):
        calls.append(argv)
        return _Ok()
    monkeypatch.setattr(subprocess, "run", _record)
    ovl.scan(["packages/admin/evolve_admin/deploy.py"])
    assert len(calls) == 1
    assert calls[0][:4] == ["gh", "pr", "list", "--state"]
    assert "files" in calls[0][-1]


# ── Rendering ────────────────────────────────────────────────────────────


def test_render_caps_the_path_list():
    paths = [f"packages/admin/f{i}.py" for i in range(10)]
    result = ovl.Scan(overlaps=[ovl.Overlap(number=9, title="big one",
                                            branch="b", paths=paths)])
    lines = ovl.render(result, max_paths=3)
    assert any("… and 7 more" in ln for ln in lines)
    assert sum(1 for ln in lines if ln.strip().startswith("packages/")) == 3
