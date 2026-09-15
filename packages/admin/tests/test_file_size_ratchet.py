"""Self-test for tools/file-size-ratchet — the diff-relative no-growth cap.

The gate freezes a handful of hot hazard files (server.py / cli.py /
routes_admin.py / deploy.py …) at a line-count baseline. The property under
test is the *diff-relative* contract (the fix for the "one over-cap merge reds
every subsequent PR" failure, memory [[green_pr_reds_main_on_whole_repo_ratchet]]):

  A branch fails ONLY if it grows a capped file past max(cap, count-on-base).
  A file already over cap on the base does not fail a branch that leaves it
  alone or shrinks it — only the branch that actually grows it pays.

Each test stands up a throwaway git repo with the *real* tool copied in (so its
``Path(__file__).parent.parent`` REPO_ROOT and its ``git show`` plumbing run for
real against that repo), commits a base, mutates the working tree, then runs the
tool with an explicit ``--base`` sha — exactly how CI invokes it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_TOOL = _REPO_ROOT / "tools" / "file-size-ratchet"


def _lines(n: int) -> str:
    """A file body with exactly ``n`` newlines (so wc -l / the tool see n)."""
    return "x\n" * n


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _setup(
    tmp_path: Path,
    caps: dict[str, int],
    base: dict[str, int],
    head: dict[str, int],
) -> tuple[Path, str]:
    """Build a git repo: baseline caps `caps`, files at `base` counts committed,
    then rewritten to `head` counts in the working tree. Returns (repo, base_sha).
    A file in `caps`/`head` but absent from `base` is created only at head."""
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(_REAL_TOOL, repo / "tools" / "file-size-ratchet")
    baseline = "# test baseline\n" + "".join(
        f"{cap}\t{rel}\n" for rel, cap in caps.items()
    )
    (repo / "tools" / "file-size-baseline.txt").write_text(baseline)

    for rel, n in base.items():
        (repo / rel).write_text(_lines(n))
    # Make sure the base commit is non-empty even if `base` is sparse.
    (repo / "README").write_text("seed\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Mutate working tree to the head state (uncommitted — mirrors a PR's
    # merge checkout, where the working tree already reflects the change).
    for rel, n in head.items():
        (repo / rel).write_text(_lines(n))
    return repo, base_sha


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo / "tools" / "file-size-ratchet"), *args],
        capture_output=True, text=True, cwd=repo,
    )


# --- the headline fix: an untouched over-cap file does not fail a branch ------

def test_untouched_file_over_cap_passes(tmp_path):
    # server.py is over its cap on main from a prior merge; this branch leaves
    # it alone. It MUST pass — this is the whole point of the change.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 15}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not this branch's debt" in r.stdout


def test_shrink_toward_cap_still_over_passes(tmp_path):
    # Over cap on base (15), this branch trims to 12 — still over the cap of 10
    # but strictly smaller. Moving toward compliance must never fail.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 12}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr


def test_grow_within_cap_passes(tmp_path):
    # Under cap on base (5) and still under after growth (9). Allowed.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 5}, head={"app.py": 9}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 0, r.stdout + r.stderr


# --- still catches real growth ------------------------------------------------

def test_grow_past_cap_fails(tmp_path):
    # Under cap on base (8), this branch pushes it over (12). MUST fail.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 8}, head={"app.py": 12}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app.py" in r.stdout


def test_grow_further_while_over_cap_fails(tmp_path):
    # Already over cap on base (15) AND this branch grows it MORE (18). The
    # branch is still on the hook for the increment it added.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 18}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "on top of an over-cap file" in r.stdout


def test_newly_added_capped_file_checked_against_cap(tmp_path):
    # A capped file absent on the base (base_n -> 0) is judged against its cap,
    # not given a free pass. Added at 12 with a cap of 10 -> fail.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={}, head={"app.py": 12}
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "app.py" in r.stdout


# --- adversarial: splitting a grow across two capped files is still caught ----

def test_split_growth_across_two_files_both_caught(tmp_path):
    # Spreading +8 lines across two capped files (each +4 over its own cap)
    # does not evade the gate — each file is judged independently.
    repo, base = _setup(
        tmp_path,
        caps={"a.py": 10, "b.py": 10},
        base={"a.py": 8, "b.py": 8},
        head={"a.py": 12, "b.py": 12},
    )
    r = _run(repo, "--all", "--base", base)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "a.py" in r.stdout and "b.py" in r.stdout


# --- fail-closed when no base is resolvable -----------------------------------

def test_no_base_falls_back_to_absolute_cap(tmp_path):
    # No --base, no origin/main in this throwaway repo -> diff-relative is off
    # and the stricter absolute cap applies. A committed file over cap fails,
    # so the gate is never a silent hole when the base can't be determined.
    # base == head (15) so the committed tree is itself over cap; with no base
    # ref resolvable the absolute cap applies to that committed state.
    repo, _ = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 15}
    )
    r = _run(repo, "--all")
    assert "absolute-cap mode" in r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


def test_unresolvable_explicit_base_does_not_silently_pass(tmp_path):
    # An explicit but bogus --base must NOT enable a free pass — it falls back
    # to the absolute cap rather than silently mis-comparing.
    repo, _ = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 15}
    )
    r = _run(repo, "--all", "--base", "deadbeefdeadbeef")
    assert "absolute-cap mode" in r.stdout
    assert r.returncode == 1, r.stdout + r.stderr


# --- env-var wiring (CI's fallback path) + shrink re-freeze --------------------

def test_base_from_env_var(tmp_path):
    # CI sets the base sha; the tool also reads $RATCHET_BASE_SHA when --base is
    # omitted. Over cap on base, unchanged here -> pass via the env path.
    repo, base = _setup(
        tmp_path, caps={"app.py": 10}, base={"app.py": 15}, head={"app.py": 15}
    )
    import os

    env = {**os.environ, "RATCHET_BASE_SHA": base}
    r = subprocess.run(
        [sys.executable, str(repo / "tools" / "file-size-ratchet"), "--all"],
        capture_output=True, text=True, cwd=repo, env=env,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "diff-relative mode" in r.stdout


def test_update_baseline_refreezes_down(tmp_path):
    # The shrink path is preserved: --update-baseline re-freezes to current
    # counts (here a file now smaller than its cap ratchets the baseline DOWN).
    repo, _ = _setup(
        tmp_path, caps={"app.py": 20}, base={"app.py": 12}, head={"app.py": 12}
    )
    r = _run(repo, "--update-baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    written = (repo / "tools" / "file-size-baseline.txt").read_text()
    assert "12\tapp.py" in written
