"""Unit tests for tools/pm-landing.

The D-PM9 lander: the PM writes `internal/_pm-landing/<slug>.json` into the operator's
checkout, and a scheduled task on the operator's laptop turns it into a branch, a commit
and a PR. The paste-script pattern it replaces cost nine operator pastes in three days.

What this file pins hardest is the three things the tool is built NEVER to do, because each
one is an incident from the week of 2026-09-01 and each one was a write to the operator's
MAIN checkout:

  * the main checkout's index is never touched — the commit is built in a throwaway
    worktree, so a `git add -A`, a GitHub Desktop auto-stash and a session in the main
    checkout are all outside the blast radius;
  * `--sweep` never unlinks a TRACKED file, and never one whose bytes differ from
    `origin/main`;
  * a brief the dispatcher already moved to `inflight/` is never re-landed from `queued/`,
    because one id in two lane dirs is `blocked_by: "lane-conflict"` and stops the lane.

These run against a REAL git repo with a real bare `origin`, so the worktree/push/branch
mechanics are exercised rather than mocked. Only `gh` is a stub — it is the one dependency
that would reach the network.

The tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "pm-landing"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("pm_landing", str(_TOOL))
    spec = importlib.util.spec_from_loader("pm_landing", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pm_landing"] = mod
    loader.exec_module(mod)
    return mod


MOD = _load_tool()


FAKE_GH = '''#!/usr/bin/env python3
"""Stand-in for `gh`, keyed on a JSON state file so a test can pre-seed or inspect it.

Supports exactly the two calls tools/pm-landing makes: `pr create` and `pr list --head`.
`PM_LANDING_FAKE_GH_FAIL=1` makes `pr create` fail the way a missing credential does, which
is the branch-pushed-but-no-PR path the tool has to recover from on the next run.
"""
import json, os, sys

state_path = os.environ["PM_LANDING_FAKE_GH_STATE"]
try:
    state = json.load(open(state_path))
except Exception:
    state = {}

argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


if argv[:2] == ["pr", "create"]:
    if os.environ.get("PM_LANDING_FAKE_GH_FAIL") == "1":
        sys.stderr.write("gh: no credential\\n")
        sys.exit(1)
    branch = opt("--head")
    number = state.get("_next", 4100)
    state["_next"] = number + 1
    url = "https://github.com/o/r/pull/%d" % number
    state[branch] = {"number": number, "url": url, "state": "OPEN",
                     "title": opt("--title"), "body": opt("--body")}
    json.dump(state, open(state_path, "w"))
    sys.stdout.write(url + "\\n")
    sys.exit(0)

if argv[:2] == ["pr", "list"]:
    row = state.get(opt("--head"))
    sys.stdout.write(json.dumps([row] if row else []) + "\\n")
    sys.exit(0)

sys.stderr.write("fake gh: unsupported %r\\n" % (argv,))
sys.exit(1)
'''


def _git(cwd, *args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, r.stderr))
    return r


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """A bare `origin`, a clone of it, a manifest dir, and a `gh` on PATH."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))

    repo = tmp_path / "checkout"
    _git(tmp_path, "clone", str(origin), str(repo))
    _git(repo, "config", "user.email", "pm@example.invalid")
    _git(repo, "config", "user.name", "PM Test")

    (repo / "internal").mkdir()
    (repo / "internal" / "seed.md").write_text("seed\n", encoding="utf-8")
    # The tool reads the scrub guard's token tuple out of the checkout it is landing from.
    # The rig gets a stand-in with invented tokens: the real list is pinned separately
    # against the real repo, and spelling the real tokens here would red the guard itself.
    guard = repo / MOD.SCRUB_GUARD_REL
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text('%s = ("quokka", "zebrafish")\n' % MOD.SCRUB_TOKENS_NAME,
                     encoding="utf-8")
    for sub in ("queued", "inflight", "done"):
        (repo / "internal" / "dispatch" / sub).mkdir(parents=True)
        (repo / "internal" / "dispatch" / sub / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin", "main")

    root = repo / "internal" / "_pm-landing"
    (root / "done").mkdir(parents=True)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    state = tmp_path / "gh-state.json"
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]))
    monkeypatch.setenv("PM_LANDING_FAKE_GH_STATE", str(state))

    return {"origin": origin, "repo": repo, "root": root, "gh_state": state}


def _manifest(rig, slug="notes", **over):
    data = {
        "branch": "pm/%s" % slug,
        "title": "PM: %s" % slug,
        "body": "## Independent two-pass review\n\nPM output.\n",
        "commit_message": "PM: %s\n\nA staged doc.\n" % slug,
        "files": ["internal/%s.md" % slug],
        "expect_untracked": ["internal/%s.md" % slug],
        "co_authored_by": "Claude Fable 5.1 <noreply@example.invalid>",
    }
    data.update(over)
    (rig["root"] / ("%s.json" % slug)).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def _write(rig, rel, text):
    p = rig["repo"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _land(rig, **kw):
    return MOD.land(rig["repo"], rig["root"], now="2026-09-04T00:00:00Z", **kw)


def _index_state(repo):
    """Everything about the main checkout that a careless tool would disturb."""
    return (_git(repo, "status", "--porcelain").stdout,
            _git(repo, "diff", "--cached", "--name-status").stdout,
            _git(repo, "rev-parse", "HEAD").stdout,
            _git(repo, "symbolic-ref", "HEAD").stdout)


# ------------------------------------------------------------------ the happy path


def test_manifest_lands_branch_with_byte_identical_content(rig):
    body = "# Notes\n\nnon-ascii: café — and a trailing space \n"
    _write(rig, "internal/notes.md", body)
    _manifest(rig)

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["landed"]
    assert out["poke"] is True
    landed = out["landed"][0]
    assert landed["pr"] == 4100

    # The branch is on origin, and the blob it carries is the file the PM wrote — byte for
    # byte, which is the property a paste script could never prove.
    shown = _git(rig["repo"], "show", "origin/pm/notes:internal/notes.md")
    assert shown.stdout == body

    msg = _git(rig["repo"], "log", "-1", "--format=%B", "origin/pm/notes").stdout
    assert "PM: notes" in msg
    assert "Landed-by: tools/pm-landing" in msg
    assert "Co-authored-by: Claude Fable 5.1 <noreply@example.invalid>" in msg

    # Exactly the listed path changed — no `add -A` sweeping the seed or the manifest.
    changed = _git(rig["repo"], "diff", "--name-only",
                   "origin/main", "origin/pm/notes").stdout.split()
    assert changed == ["internal/notes.md"]


def test_manifest_moves_to_done_with_the_pr_number(rig):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    _land(rig)

    assert not (rig["root"] / "notes.json").exists()
    done = json.loads((rig["root"] / "done" / "notes.json").read_text(encoding="utf-8"))
    assert done["pr"] == 4100
    assert done["pr_url"].endswith("/pull/4100")
    assert done["landed_at"] == "2026-09-04T00:00:00Z"
    assert done["files"] == ["internal/notes.md"]


def test_the_main_checkouts_index_is_never_touched(rig):
    """The guardrail the three 2026-09 incidents bought."""
    _write(rig, "internal/notes.md", "x\n")
    _write(rig, "internal/operator-scratch.md", "the operator's own uncommitted work\n")
    _git(rig["repo"], "add", "internal/operator-scratch.md")
    _manifest(rig)

    before = _index_state(rig["repo"])
    _land(rig)
    after = _index_state(rig["repo"])

    assert before == after
    # And their staged file is still staged, unmodified, and NOT in the landed commit.
    assert (rig["repo"] / "internal" / "operator-scratch.md").read_text(encoding="utf-8") \
        == "the operator's own uncommitted work\n"
    changed = _git(rig["repo"], "diff", "--name-only",
                   "origin/main", "origin/pm/notes").stdout
    assert "operator-scratch" not in changed


def test_the_worktree_is_removed_after_landing(rig):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    _land(rig)

    assert not (rig["repo"] / ".worktrees" / "pm-notes").exists()
    assert "pm-notes" not in _git(rig["repo"], "worktree", "list").stdout


def test_a_lane_brief_lands_and_carries_its_body(rig):
    """The common case: the PM queues a brief. Nothing about the lane blocks it."""
    brief = "---\nid: a-new-brief\naspect: substrate\n---\nWHY: because.\n"
    _write(rig, "internal/dispatch/queued/a-new-brief.md", brief)
    _manifest(rig, slug="queue-a-brief",
              files=["internal/dispatch/queued/a-new-brief.md"],
              expect_untracked=["internal/dispatch/queued/a-new-brief.md"])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["landed"]
    shown = _git(rig["repo"], "show",
                 "origin/pm/queue-a-brief:internal/dispatch/queued/a-new-brief.md")
    assert shown.stdout == brief


# ------------------------------------------------------------------ refusals


def test_refuses_a_path_under_inflight(rig):
    _write(rig, "internal/dispatch/inflight/x.md", "marker\n")
    _manifest(rig, slug="marker", files=["internal/dispatch/inflight/x.md"],
              expect_untracked=[])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "inflight" in out["refused"][0]["reason"]
    assert out["poke"] is True
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/marker").stdout == ""


def test_refuses_a_queued_brief_whose_id_is_in_flight(rig):
    """The dispatcher moved it between the PM staging the manifest and this run."""
    _write(rig, "internal/dispatch/queued/dup.md", "---\nid: dup\n---\nWHY: x.\n")
    _write(rig, "internal/dispatch/inflight/dup.md", "---\nid: dup\n---\nWHY: x.\n")
    _manifest(rig, slug="dup-brief", files=["internal/dispatch/queued/dup.md"],
              expect_untracked=[])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "lane-conflict" in out["refused"][0]["reason"]


def test_a_queued_brief_lands_when_its_id_is_only_in_done(rig):
    """`done/` is not `inflight/` — the refusal is narrow on purpose."""
    _write(rig, "internal/dispatch/done/old.md", "---\nid: old\n---\nWHY: x.\n")
    _write(rig, "internal/dispatch/queued/other.md", "---\nid: other\n---\nWHY: y.\n")
    _manifest(rig, slug="ok-brief", files=["internal/dispatch/queued/other.md"],
              expect_untracked=[])

    assert [r["status"] for r in _land(rig)["results"]] == ["landed"]


@pytest.mark.parametrize("rel", [
    "packages/admin/evolve_admin/server.py",
    "tools/pm-landing",
    "README.md",
    "config/network.json",
])
def test_refuses_a_path_outside_the_allowlist(rig, rel):
    _write(rig, rel, "x\n")
    _manifest(rig, slug="outside", files=[rel], expect_untracked=[])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "outside" in out["refused"][0]["reason"]


@pytest.mark.parametrize("rel", ["/etc/passwd", "internal/../.git/config",
                                 "internal//x.md", "docs/", "internal/x.md "])
def test_refuses_a_path_that_escapes_or_is_malformed(rig, rel):
    _manifest(rig, slug="escape", files=[rel], expect_untracked=[])
    assert [r["status"] for r in _land(rig)["results"]] == ["refused"]


def test_refuses_when_the_branch_name_is_taken_by_something_else(rig):
    """Same name, different provenance — a PR opened here would be somebody else's work."""
    _write(rig, "internal/notes.md", "x\n")
    _git(rig["repo"], "branch", "pm/notes", "origin/main")
    _git(rig["repo"], "push", "origin", "pm/notes")
    _git(rig["repo"], "branch", "-D", "pm/notes")
    _manifest(rig)

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "Landed-by" in out["refused"][0]["reason"]
    # And the manifest stays put, so the operator's rename is what unblocks it.
    assert (rig["root"] / "notes.json").exists()


def test_refuses_a_manifest_with_an_unknown_key(rig):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    data = json.loads((rig["root"] / "notes.json").read_text(encoding="utf-8"))
    data["file"] = ["internal/notes.md"]          # the typo that lands an empty commit
    (rig["root"] / "notes.json").write_text(json.dumps(data), encoding="utf-8")

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "unknown key" in out["refused"][0]["reason"]


def test_refuses_expect_untracked_that_files_does_not_list(rig):
    """The sweep may only ever delete a path this manifest landed."""
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig, expect_untracked=["internal/notes.md", "internal/seed.md"])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "expect_untracked" in out["refused"][0]["reason"]


def test_refuses_a_manifest_whose_file_is_not_in_the_checkout(rig):
    _manifest(rig, slug="ghost", files=["internal/ghost.md"], expect_untracked=[])
    out = _land(rig)
    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "not a regular file" in out["refused"][0]["reason"]


def test_one_bad_manifest_does_not_stop_the_others(rig):
    _write(rig, "internal/good.md", "g\n")
    _manifest(rig, slug="good", files=["internal/good.md"],
              expect_untracked=["internal/good.md"])
    _manifest(rig, slug="bad", files=["packages/x.py"], expect_untracked=[])

    out = _land(rig)

    assert {r["slug"]: r["status"] for r in out["results"]} == {
        "good": "landed", "bad": "refused"}


# ------------------------------------------------------------------ idempotence


def test_a_re_run_with_nothing_pending_is_a_no_op(rig):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    first = _land(rig)
    before = _index_state(rig["repo"])

    second = _land(rig)

    assert second["results"] == []
    assert second["poke"] is False
    assert _index_state(rig["repo"]) == before
    assert first["landed"][0]["pr"] == 4100
    assert json.loads(rig["gh_state"].read_text(encoding="utf-8"))["_next"] == 4101


def test_a_branch_this_manifest_already_pushed_is_not_pushed_again(rig):
    """The crash-recovery path: the previous run pushed, then died before the PR."""
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    os.environ["PM_LANDING_FAKE_GH_FAIL"] = "1"
    try:
        first = _land(rig)
    finally:
        del os.environ["PM_LANDING_FAKE_GH_FAIL"]

    assert first["results"][0]["status"] == "landed-no-pr"
    assert (rig["root"] / "notes.json").exists()          # bookkeeping still owed
    tip = _git(rig["repo"], "rev-parse", "origin/pm/notes").stdout

    second = _land(rig)

    assert second["results"][0]["status"] == "skipped"
    assert second["results"][0]["reason"] == "branch-exists"
    assert second["results"][0]["pr"] == 4100
    assert _git(rig["repo"], "rev-parse", "origin/pm/notes").stdout == tip
    done = json.loads((rig["root"] / "done" / "notes.json").read_text(encoding="utf-8"))
    assert done["pr"] == 4100


def test_files_already_identical_to_main_land_nothing_and_open_no_pr(rig):
    _manifest(rig, slug="seeded", files=["internal/seed.md"],
              expect_untracked=["internal/seed.md"])

    out = _land(rig)

    assert out["results"][0]["reason"] == "already-on-main"
    assert out["poke"] is False
    assert not rig["gh_state"].exists()            # `gh` was never invoked at all
    assert (rig["root"] / "done" / "seeded.json").exists()
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/seeded").stdout == ""


def test_refuses_to_overwrite_a_done_record_from_a_different_landing(rig):
    """`--sweep` reads `done/` to decide what it may delete. A reused slug would re-point
    that authority at files the old PR never carried."""
    _write(rig, "internal/notes.md", "first\n")
    _manifest(rig)
    _land(rig)

    _write(rig, "internal/later.md", "second\n")
    _manifest(rig, slug="notes", branch="pm/notes-again",
              files=["internal/later.md"], expect_untracked=["internal/later.md"])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "already landed" in out["refused"][0]["reason"]
    done = json.loads((rig["root"] / "done" / "notes.json").read_text(encoding="utf-8"))
    assert done["files"] == ["internal/notes.md"]        # the first record survives intact
    assert done["pr"] == 4100
    # And it refused BEFORE pushing: a refusal discovered inside the manifest move would
    # already have opened a PR nobody meant to open, and wedged the slug behind it.
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/notes-again").stdout == ""
    assert json.loads(rig["gh_state"].read_text(encoding="utf-8"))["_next"] == 4101


def test_dry_run_changes_nothing(rig):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    before = _index_state(rig["repo"])

    out = _land(rig, dry_run=True)

    assert out["results"][0]["reason"] == "dry-run"
    assert (rig["root"] / "notes.json").exists()
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/notes").stdout == ""
    assert _index_state(rig["repo"]) == before


# ------------------------------------------------------------------ the sweep


def _landed_and_merged(rig, slug="notes", body="x\n"):
    """Land a manifest and fast-forward `origin/main` onto it, as a merge would."""
    _write(rig, "internal/%s.md" % slug, body)
    _manifest(rig, slug=slug)
    _land(rig)
    _git(rig["repo"], "push", "origin", "origin/pm/%s:refs/heads/main" % slug)
    _git(rig["repo"], "fetch", "origin", "main")


def test_sweep_deletes_the_untracked_copy_once_main_carries_it(rig):
    _landed_and_merged(rig)
    assert (rig["repo"] / "internal" / "notes.md").exists()

    out = MOD.sweep(rig["repo"], rig["root"])

    assert out["deleted"] == ["internal/notes.md"]
    assert not (rig["repo"] / "internal" / "notes.md").exists()
    assert out["poke"] is False
    done = json.loads((rig["root"] / "done" / "notes.json").read_text(encoding="utf-8"))
    assert done["swept"] == ["internal/notes.md"]


def test_sweep_leaves_a_file_main_does_not_have_yet(rig):
    """The PR is open, not merged. Deleting now would destroy the only copy."""
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    _land(rig)

    out = MOD.sweep(rig["repo"], rig["root"])

    assert out["deleted"] == []
    assert (rig["repo"] / "internal" / "notes.md").exists()
    assert out["results"][0]["kept"][0]["why"].startswith("not on origin/main")


def test_sweep_never_deletes_a_tracked_file(rig):
    """The 2026-09-02 incident. A tracked delete is a staged change in the main checkout."""
    _landed_and_merged(rig)
    _git(rig["repo"], "add", "internal/notes.md")

    out = MOD.sweep(rig["repo"], rig["root"])

    assert out["deleted"] == []
    assert (rig["repo"] / "internal" / "notes.md").exists()
    assert "tracked" in out["results"][0]["kept"][0]["why"]


def test_sweep_leaves_a_copy_the_operator_edited_after_it_landed(rig):
    _landed_and_merged(rig)
    (rig["repo"] / "internal" / "notes.md").write_text("x\nlocal edit\n", encoding="utf-8")

    out = MOD.sweep(rig["repo"], rig["root"])

    assert out["deleted"] == []
    assert "differs" in out["results"][0]["kept"][0]["why"]
    assert (rig["repo"] / "internal" / "notes.md").read_text(encoding="utf-8") \
        == "x\nlocal edit\n"


def test_sweep_only_considers_files_the_manifest_landed(rig):
    _landed_and_merged(rig)
    stray = _write(rig, "internal/unrelated.md", "not mine\n")

    MOD.sweep(rig["repo"], rig["root"])

    assert stray.exists()


def test_sweep_is_idempotent(rig):
    _landed_and_merged(rig)
    first = MOD.sweep(rig["repo"], rig["root"])
    second = MOD.sweep(rig["repo"], rig["root"])

    assert first["deleted"] == ["internal/notes.md"]
    assert second["deleted"] == []
    assert second["poke"] is False


def test_sweep_dry_run_deletes_nothing(rig):
    _landed_and_merged(rig)

    out = MOD.sweep(rig["repo"], rig["root"], dry_run=True)

    assert out["deleted"] == ["internal/notes.md"]
    assert (rig["repo"] / "internal" / "notes.md").exists()


def test_sweep_pokes_on_a_done_manifest_that_no_longer_parses(rig):
    (rig["root"] / "done" / "broken.json").write_text("{not json", encoding="utf-8")

    out = MOD.sweep(rig["repo"], rig["root"])

    assert out["poke"] is True
    assert "not valid JSON" in out["refused"][0]["reason"]


# ------------------------------------------------------------------ CLI surface


def test_cli_json_reports_the_poke_flag(rig, capsys, monkeypatch):
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    monkeypatch.chdir(rig["repo"])

    rc = MOD.main(["--dir", str(rig["root"]), "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["poke"] is True
    assert out["landed"][0]["slug"] == "notes"


def test_cli_exits_2_when_the_manifest_dir_is_absent(rig, capsys, monkeypatch):
    monkeypatch.chdir(rig["repo"])
    assert MOD.main(["--dir", "internal/_nope"]) == MOD.EXIT_REFUSED
    assert "no manifest dir" in capsys.readouterr().err


def test_cli_sweep_mode(rig, capsys, monkeypatch):
    _landed_and_merged(rig)
    monkeypatch.chdir(rig["repo"])

    rc = MOD.main(["--dir", str(rig["root"]), "--sweep", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == ["internal/notes.md"]


# ------------------------------------------------------ secrets, ignored paths, the scrub


def test_refuses_a_path_under_docs_private(rig):
    """`docs/private/` holds the operator's local-only material, the PM's token included.

    The stop cannot be `.gitignore`: by the time `git add` drops an ignored path the bytes
    have already been read out of the checkout and written into a worktree.
    """
    _manifest(rig, slug="leak", files=["docs/private/x"], expect_untracked=[])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "private" in out["refused"][0]["reason"]
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/leak").stdout == ""


def test_refuses_a_git_ignored_path_before_reading_it(rig):
    """The general case behind `docs/private/`: an ignored path cannot land anyway."""
    _write(rig, ".gitignore", "internal/local-*.md\n")
    _write(rig, "internal/local-notes.md", "operator-only\n")
    _manifest(rig, slug="ignored", files=["internal/local-notes.md"], expect_untracked=[])

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "ignored" in out["refused"][0]["reason"]
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/ignored").stdout == ""


def test_refuses_a_payload_carrying_a_reserved_token(rig):
    """The scrub guard reds CI on these, and a pushed PM branch is never re-pushed — so a
    PR that trips it cannot be fixed through this tool. The scan runs before the push."""
    token = MOD.reserved_tokens(rig["repo"])[0]
    _write(rig, "internal/notes.md", "# Notes\n\nthe pod runs %s nightly\n" % token)
    _manifest(rig)

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    reason = out["refused"][0]["reason"]
    assert token in reason and "internal/notes.md:3" in reason
    assert out["poke"] is True
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/notes").stdout == ""
    assert not rig["gh_state"].exists()


def test_the_reserved_token_scan_is_whole_word_and_case_insensitive(rig):
    token = MOD.reserved_tokens(rig["repo"])[0]
    payload = {"internal/a.md": ("a %s-like shape\n" % token.upper()).encode("utf-8")}
    with pytest.raises(MOD.Refused):
        MOD.check_no_reserved_tokens(payload, MOD.reserved_tokens(rig["repo"]))

    # ...but a token that is only a SUBSTRING is not a violation, because it is not one to
    # the guard either — refusing it would refuse manifests CI would have passed.
    ok = {"internal/a.md": ("%sish\n" % token).encode("utf-8")}
    MOD.check_no_reserved_tokens(ok, MOD.reserved_tokens(rig["repo"]))


def test_the_scan_reads_the_real_guards_token_list(rig):
    """Extraction pin: the tool must read the SAME tuple the CI guard enforces.

    Read out of the guard's source with `ast` rather than imported (the guard needs pytest,
    which an unattended `python3 tools/pm-landing` run does not have) and rather than
    copied (a copy drifts, and would put the reserved words in a tracked file).
    """
    import importlib

    scrub = importlib.import_module("tests.test_public_launch_scrub")
    real_repo = Path(__file__).resolve().parents[3]

    assert MOD.reserved_tokens(real_repo) == tuple(scrub.RESERVED_TOKENS)


def test_refuses_rather_than_landing_an_unscanned_payload(rig):
    """No token list, no landing — the alternative is a red PR the tool cannot repair."""
    (rig["repo"] / MOD.SCRUB_GUARD_REL).unlink()
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)

    out = _land(rig)

    assert [r["status"] for r in out["results"]] == ["refused"]
    assert "unscanned" in out["refused"][0]["reason"]
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/notes").stdout == ""


# ------------------------------------------------------ the stuck states are not silent


def test_a_branch_that_exists_with_no_pr_pokes_instead_of_going_quiet(rig):
    """Push succeeded, `gh` never did. Reported as `skipped` this repeated every 30
    minutes with nobody told; it is the same STATE as a first run that could not open the
    PR, so it reports as `landed-no-pr` and pokes once."""
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    os.environ["PM_LANDING_FAKE_GH_FAIL"] = "1"
    try:
        first = _land(rig)
        assert first["results"][0]["status"] == "landed-no-pr"

        second = _land(rig)
    finally:
        del os.environ["PM_LANDING_FAKE_GH_FAIL"]

    assert second["results"][0]["status"] == "landed-no-pr"
    assert second["results"][0]["reason"] == "branch-exists-pr-failed"
    assert second["poke"] is True
    assert (rig["root"] / "notes.json").exists()      # bookkeeping is still owed


def test_cli_refuses_to_run_in_the_deploy_checkout(rig, capsys, monkeypatch):
    """The repo-puller pulls that checkout `--ff-only` every 15 minutes and the files this
    tool writes there are untracked — which is what wedges the pull."""
    _write(rig, "internal/notes.md", "x\n")
    _manifest(rig)
    monkeypatch.setattr(MOD, "DEPLOY_CHECKOUT", str(rig["repo"]))
    monkeypatch.chdir(rig["repo"])

    rc = MOD.main(["--dir", str(rig["root"]), "--json"])

    assert rc == MOD.EXIT_REFUSED
    assert "deploy checkout" in capsys.readouterr().err
    assert (rig["root"] / "notes.json").exists()
    assert _git(rig["repo"], "ls-remote", "--heads", "origin", "pm/notes").stdout == ""
