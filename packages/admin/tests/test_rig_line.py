"""`tools/rig_line.py` — the D-TP10 rig line.

Driven by a FAKE runner (no `gh`, no network, no real git history) over one fixture day,
2026-09-20 12:00Z. Pins the rendered line, the 48-hour list with each PR's owner lane and
one action, and that a source the token cannot read renders `unknown` — never zero.
Every name here is a placeholder.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "tools"))
import rig_line as rl  # noqa: E402

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _pr(n, ref, created, *, login="placeholder-dev", merged=None, sha=None):
    return {"number": n, "head": {"ref": ref, "sha": sha or "sha%d" % n},
            "user": {"login": login}, "created_at": created, "merged_at": merged}


OPEN = [
    _pr(101, "claude/meta-widget-one", "2026-09-17T12:00:00Z"),            # 72h
    _pr(102, "claude/placeholder-app", "2026-09-19T12:00:00Z"),            # 24h
    _pr(103, "pm/alpha-state", "2026-09-18T06:00:00Z"),                    # 54h
    _pr(104, "dependabot/npm/thing", "2026-09-01T12:00:00Z",
        login="dependabot[bot]"),                                          # 456h
    _pr(105, "claude/meta-widget-two", "2026-09-18T10:00:00Z"),            # 50h
]
RECENT = OPEN + [
    _pr(90, "claude/meta-a", "2026-09-19T20:00:00Z", merged="2026-09-20T01:00:00Z"),
    _pr(91, "pm/beta", "2026-09-19T09:00:00Z", merged="2026-09-19T15:00:00Z"),
    _pr(92, "claude/app-x", "2026-09-18T09:00:00Z", merged="2026-09-19T10:00:00Z"),
    _pr(93, "dependabot/pip/y", "2026-09-19T09:00:00Z", login="dependabot[bot]",
        merged="2026-09-20T09:00:00Z"),
    _pr(94, "claude/meta-b", "2026-09-20T08:00:00Z"),                     # closed, unmerged
]
MAIN_RUNS = [
    {"id": 1, "name": "ci", "conclusion": "success", "updated_at": "2026-09-19T08:00:00Z"},
    {"id": 2, "name": "ci", "conclusion": "failure", "updated_at": "2026-09-19T20:00:00Z"},
    {"id": 3, "name": "ci", "conclusion": "failure", "updated_at": "2026-09-19T22:00:00Z"},
    {"id": 4, "name": "ci", "conclusion": "success", "updated_at": "2026-09-20T06:00:00Z"},
    {"id": 5, "name": "publish-public", "conclusion": "failure",
     "updated_at": "2026-09-20T07:00:00Z"},
]
JOBS = {2: [{"name": "Admin suite", "conclusion": "failure"}],
        3: [{"name": "Flaky e2e", "conclusion": "failure"}]}   # quarantined below
PR_RUNS = {"sha104": [{"name": "ci", "status": "completed", "conclusion": "failure",
                       "updated_at": "2026-09-19T00:00:00Z"}],
           "sha101": [{"name": "ci", "status": "completed", "conclusion": "failure",
                       "updated_at": "2026-09-17T13:00:00Z"},
                      {"name": "ci", "status": "completed", "conclusion": "success",
                       "updated_at": "2026-09-18T13:00:00Z"}],
           "sha103": []}
MERGEABLE = {101: "clean", 103: "dirty", 104: "dirty", 105: "unknown"}
OLD_STATE = "# PM\n## NEEDS THE OPERATOR (open)\n1. Tap the card.\n2. Rule on the tier.\n"
NEW_STATE = ("# PM\n## NEEDS THE OPERATOR (open)\n1. Rule on the tier.\n"
             "2. Install the probe.\n## Working notes\n- not an ask\n")
HEARTBEATS = {
    "2026-09-19": ["meta-tick: blocked: stale-checkout; pokes 0",          # 11:02 outside
                   "meta-tick: blocked: stale-checkout; pokes 0",          # 13:02 -> A
                   "meta-tick: stopped at rig preflight (may_reconcile: false); "
                   "block:AE; pokes 1",                                    # 14:02 -> A, E
                   "meta-tick: blocked: back-pressure; pokes 0"],          # 14:40 -> C
    "2026-09-20": ["meta-tick: blocked: cap; pokes 0",                     # 02:02 -> none
                   "meta-tick: stopped at rig preflight (may_reconcile: false); "
                   "pokes 0"],                                             # 03:02 -> ?
}
RUN_AT = {"2026-09-19": ["11:02", "13:02", "14:02", "14:40"],
          "2026-09-20": ["02:02", "03:02"]}


class Fake:
    def __init__(self, deny=()):
        self.deny, self.argvs = tuple(deny), []

    def answer(self, argv):
        if argv[0] == "git":
            return "abc123\n" if argv[1] == "log" else OLD_STATE
        path = argv[2].split("repos/{owner}/{repo}/", 1)[1]
        if any(path.startswith(d) for d in self.deny):
            return None
        if path.startswith("pulls?state=open"):
            return OPEN
        if path.startswith("pulls?state=all"):
            return RECENT
        if path.startswith("pulls/"):
            return {"mergeable_state": MERGEABLE.get(int(path.split("/")[1]))}
        if path.startswith("actions/runs?head_sha="):
            sha = path.split("=", 1)[1].split("&")[0]
            return None if sha == "sha105" else {"workflow_runs": PR_RUNS.get(sha, [])}
        if path.startswith("actions/runs?event=push"):
            return {"workflow_runs": MAIN_RUNS}
        if "/jobs" in path:
            return {"jobs": JOBS.get(int(path.split("/")[2]), [])}
        raise AssertionError(path)

    def __call__(self, argv, cwd):
        self.argvs.append(argv)
        a = self.answer(argv)
        if a is None:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403")
        return types.SimpleNamespace(returncode=0, stdout=a if isinstance(a, str)
                                     else json.dumps(a), stderr="")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "internal" / "pm").mkdir(parents=True)
    (repo / "internal" / "pm" / "PM-STATE-alpha.md").write_text(NEW_STATE)
    reviews = repo / "internal" / "dispatch" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "pr-101.md").write_text("# Review\nVerdict: CONCERNS — early read.\n")
    (reviews / "pr-101-second-pass.md").write_text("**Verdict:** PASS — fine.\n")
    (reviews / "pr-105.md").write_text("Verdict: FAIL — broken.\n")
    (repo / "tools").mkdir()
    (repo / "tools" / "required-check-quarantine.txt").write_text(
        "Flaky e2e\tplatform-owner\t2099-12-31\t# placeholder row\n")
    log = tmp_path / "log"
    log.mkdir()
    for day, notes in HEARTBEATS.items():
        (log / ("%s.jsonl" % day)).write_text("".join(
            json.dumps({"run": "%sT%s:00Z" % (day, at), "note": n}) + "\n"
            for at, n in zip(RUN_AT[day], notes)))
    return repo, log


def test_the_line_on_a_fixture_day(tmp_path):
    repo, log = _repo(tmp_path)
    out = rl.report(repo, now=NOW, runner=Fake(), log_dir=log)
    assert out["line"] == (
        "rig 24h: merges 3 (chip 1 · PM 1 · app 0 · dependabot 1) · chips built 2 · "
        "open PRs 5, median age 54h · >48h 4 (#104, #101, #103, #105) · "
        "operator asks +1/−1 · main red 2.0h · lane blocked 3h (?1 A2 C1 E1)")


def test_the_48_hour_list_names_owner_lane_and_one_action(tmp_path):
    repo, log = _repo(tmp_path)
    out = rl.report(repo, now=NOW, runner=Fake(), log_dir=log)
    assert out["stale_lines"] == [
        "  48h+ #104 (456h, dependabot) → close",          # 19 days, red, dirty
        "  48h+ #101 (72h, chip widget-one) → merge",      # newest review PASS, green, clean
        "  48h+ #103 (54h, PM alpha) → fix chip",          # PM owes no review; dirty
        "  48h+ #105 (50h, chip widget-two) → fix chip",   # FAIL; its CI unreadable
    ]


def test_unreadable_checks_render_unknown_never_zero(tmp_path):
    repo, _ = _repo(tmp_path)
    line = rl.report(repo, now=NOW, runner=Fake(deny=("actions/",)),
                     log_dir=tmp_path / "absent")["line"]
    assert "main red unknown" in line and "lane blocked unknown" in line
    assert "merges 3 (" in line  # the PR list was readable, so it still counts


def test_a_failed_run_whose_jobs_are_unreadable_is_unknown_not_green(tmp_path):
    repo, log = _repo(tmp_path)
    line = rl.report(repo, now=NOW, runner=Fake(deny=("actions/runs/2/",)),
                     log_dir=log)["line"]
    assert "main red unknown" in line


def test_nothing_readable_is_all_unknown(tmp_path):
    repo, _ = _repo(tmp_path)
    line = rl.report(repo, now=NOW, runner=Fake(deny=("pulls", "actions/")),
                     log_dir=tmp_path / "absent")["line"]
    assert line.startswith("rig 24h: merges unknown · chips built unknown · open PRs "
                           "unknown, median age unknown · >48h unknown")


def test_block_classes_follow_the_decision():
    assert rl.classify_block("rig: login expired — run /login on the laptop") == "A"
    assert rl.classify_block("rig: checkout on 'x' is dirty") == "A"
    assert rl.classify_block("rig: token cannot read checks — GET x returned 403") == "E"
    assert rl.classify_block("rig: lane holds id 'x' in 2 dir(s)") == "B"
    assert rl.classify_block("back-pressure") == "C"
    assert rl.classify_block("cap") is None and rl.classify_block("prepared-cap") is None


def test_it_is_read_only(tmp_path):
    """Every command it ran is a `gh api` GET or a `git log`/`git show` — nothing else."""
    repo, log = _repo(tmp_path)
    fake = Fake()
    rl.report(repo, now=NOW, runner=fake, log_dir=log)
    for argv in fake.argvs:
        assert argv[:2] in (["gh", "api"], ["git", "log"], ["git", "show"]), argv
        assert "-X" not in argv and "--method" not in argv


def test_it_stays_under_300_lines():
    assert len((_REPO / "tools" / "rig_line.py").read_text().splitlines()) < 300
